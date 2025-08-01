"""Wrappers and utilities for storing test reports."""
import json
import os
import tempfile

from abc import ABC, abstractmethod
from azure.kusto.data import KustoConnectionStringBuilder

try:
    from azure.kusto.ingest import KustoIngestClient
except ImportError:
    from azure.kusto.ingest import QueuedIngestClient as KustoIngestClient

from azure.kusto.ingest import IngestionProperties

# Resolve azure.kusto.ingest compatibility issue
try:
    from azure.kusto.ingest import DataFormat
except ImportError:
    from azure.kusto.data.data_format import DataFormat

# Import DefaultAzureCredential for token-based authentication
try:
    from azure.identity import DefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None

from utilities import validate_json_file
from datetime import datetime
from typing import Dict, List


TASK_RESULT_FILE = "pipeline_task_results.json"


class ReportDBConnector(ABC):
    """ReportDBConnector is a wrapper for a back-end data store for JUnit test reports.

    The ReportDBConnector API is intentionally very high-level so that different data stores
    with (possibly) drastically different data models can be used interchangeably.

    Subclasses of ReportDBConnector should not add ANY data store/data model/schema specific
    details into the ReportDBConnector DB API.
    """

    @abstractmethod
    def upload_report(self, report_json: Dict, external_tracking_id: str = "", report_guid: str = "") -> None:
        """Upload a report to the back-end data store.

        Args:
            report_json: A JUnit test report in JSON format. See junit_xml_parser.
            external_tracking_id: An identifier that a client can use to map a test report
                to some external system of their choosing (e.g. Jenkins, Travis CI, JIRA, etc.).
                This id does not have to be unique.
            report_guid: A randomly generated UUID that is used to query for a specific test run across tables.
        """
        pass

    @abstractmethod
    def upload_reachability_data(self, ping_output: List) -> None:
        """Upload testbed reachability data to the back-end data store.

        Args:
            ping_output: A list of ICMP ping results from devutils.
        """
        pass

    @abstractmethod
    def upload_pdu_status_data(self, pdu_status_output: List) -> None:
        """Upload PDU status data to the back-end data store.

        Args:
            pdu_status_output: A list of PDU status results from devutils.
        """

    @abstractmethod
    def upload_reboot_report(self, path_name: str = "", report_guid: str = "") -> None:
        """Upload reboot test report to the back-end data store.

        Args:
            path_name: Path to reboot report/summary file
            report_guid: A randomly generated UUID that is used to query for a specific test run across tables.
        """

    @abstractmethod
    def upload_expected_runs(self, expected_runs: List) -> None:
        """Upload expected test runs to the back-end data store.

        Args:
            expected_runs: A list of expected runs.
        """


class KustoConnector(ReportDBConnector):
    """KustoReportDB is a wrapper for storing test reports in Kusto/Azure Data Explorer."""

    METADATA_TABLE = "TestReportMetadata"
    SWSSDATA_TABLE = "SwssInvocationReport"
    SUMMARY_TABLE = "TestReportSummary"
    RAW_CASE_TABLE = "RawTestCases"
    RAW_REACHABILITY_TABLE = "RawReachabilityData"
    TESTBEDREACHABILITY_TABLE = "TestbedReachability"
    RAW_PDU_STATUS_TABLE = "RawPduStatusData"
    RAW_REBOOT_TIMING_TABLE = "RawRebootTimingData"
    REBOOT_TIMING_TABLE = "RebootTimingData"
    TEST_CASE_TABLE = "TestCases"
    EXPECTED_TEST_RUNS_TABLE = "ExpectedTestRuns"
    TEST_CASE_NUMBERS_TABLE = "TestCaseNumbers"
    PIPELINE_TABLE = "TestReportPipeline"
    CASE_INVOC_TABLE = "CaseInvocationCoverage"
    SAI_HEADER_INVOC_TABLE = "SAIHeaderDefinition"

    TABLE_FORMAT_LOOKUP = {
        METADATA_TABLE: DataFormat.JSON,
        SWSSDATA_TABLE: DataFormat.MULTIJSON,
        SUMMARY_TABLE: DataFormat.JSON,
        RAW_CASE_TABLE: DataFormat.MULTIJSON,
        RAW_REACHABILITY_TABLE: DataFormat.MULTIJSON,
        TESTBEDREACHABILITY_TABLE: DataFormat.JSON,
        RAW_PDU_STATUS_TABLE: DataFormat.MULTIJSON,
        RAW_REBOOT_TIMING_TABLE: DataFormat.JSON,
        REBOOT_TIMING_TABLE: DataFormat.MULTIJSON,
        TEST_CASE_TABLE: DataFormat.JSON,
        EXPECTED_TEST_RUNS_TABLE: DataFormat.JSON,
        TEST_CASE_NUMBERS_TABLE: DataFormat.JSON,
        PIPELINE_TABLE: DataFormat.JSON,
        CASE_INVOC_TABLE: DataFormat.MULTIJSON,
        SAI_HEADER_INVOC_TABLE: DataFormat.MULTIJSON,
    }

    TABLE_MAPPING_LOOKUP = {
        METADATA_TABLE: "FlatMetadataMappingV1",
        SWSSDATA_TABLE: "SwssInvocationReportMapping",
        SUMMARY_TABLE: "FlatSummaryMappingV1",
        RAW_CASE_TABLE: "RawCaseMappingV1",
        RAW_REACHABILITY_TABLE: "RawReachabilityMappingV1",
        TESTBEDREACHABILITY_TABLE: "TestbedReachabilityMapping",
        RAW_PDU_STATUS_TABLE: "RawPduStatusMapping",
        RAW_REBOOT_TIMING_TABLE: "RawRebootTimingDataMapping",
        REBOOT_TIMING_TABLE: "RebootTimingDataMapping",
        TEST_CASE_TABLE: "TestCasesMappingV1",
        EXPECTED_TEST_RUNS_TABLE: "ExpectedTestRunsV1",
        TEST_CASE_NUMBERS_TABLE: "TestCaseNumbersV1",
        PIPELINE_TABLE: "FlatPipelineMappingV1",
        CASE_INVOC_TABLE: "CaseInvocationCoverageMapping",
        SAI_HEADER_INVOC_TABLE: "SAIHeaderDefinitionMapping",
    }

    def __init__(self, db_name: str, auth_method: str = "appKey"):
        """Initialize a Kusto report DB connector.

        Args:
            db_name: The Kusto database to connect to.
            auth_method: Authentication method for Kusto connection.
                Supported methods: appKey, managedId, interactive, azureCli,
                deviceCode, userToken, appToken, defaultCredential
        """
        self.db_name = db_name
        self.auth_method = auth_method

        ingest_cluster = os.getenv("TEST_REPORT_INGEST_KUSTO_CLUSTER")

        # Create connection string builder based on auth method
        kcsb = self._create_connection_string_builder(ingest_cluster, auth_method)
        self._ingestion_client = KustoIngestClient(kcsb)

        """
            Kusto performance depends on the work load of cluster,
            to improve the high availability of test result data service
            by hosting a backup cluster, which is optional.
        """
        ingest_cluster_backup = os.getenv("TEST_REPORT_INGEST_KUSTO_CLUSTER_BACKUP")

        if not ingest_cluster_backup:
            print("Could not load backup Kusto Cluster from environment")
            self._ingestion_client_backup = None
        else:
            try:
                kcsb_backup = self._create_connection_string_builder(ingest_cluster_backup, auth_method, backup=True)
                self._ingestion_client_backup = KustoIngestClient(kcsb_backup)
            except Exception as e:
                print(f"Could not create backup Kusto connection: {e}")
                self._ingestion_client_backup = None

    def _create_connection_string_builder(self, cluster: str, auth_method: str, backup: bool = False):
        """Create KustoConnectionStringBuilder based on authentication method.

        Args:
            cluster: Kusto cluster URL
            auth_method: Authentication method to use
            backup: Whether this is for backup cluster (uses different env vars)

        Returns:
            KustoConnectionStringBuilder instance
        """
        if not cluster:
            raise RuntimeError("Kusto cluster URL is required")

        env_suffix = "_BACKUP" if backup else ""

        if auth_method == "appKey":
            tenant_id = os.getenv(f"TEST_REPORT_AAD_TENANT_ID{env_suffix}")
            service_id = os.getenv(f"TEST_REPORT_AAD_CLIENT_ID{env_suffix}")
            service_key = os.getenv(f"TEST_REPORT_AAD_CLIENT_KEY{env_suffix}")

            if not tenant_id or not service_id or not service_key:
                raise RuntimeError(
                    f"Could not load Kusto AppKey credentials from environment{' (backup)' if backup else ''}"
                )

            return KustoConnectionStringBuilder.with_aad_application_key_authentication(
                cluster, service_id, service_key, tenant_id)

        elif auth_method == "managedId":
            ManagedIdentityClientId = os.getenv(f"TEST_REPORT_AAD_MANAGED_IDENTITY_CLIENT_ID{env_suffix}", None)
            if ManagedIdentityClientId:
                print(f"Using user-assigned managed identity: {ManagedIdentityClientId}")
            else:
                print("Using system-assigned managed identity")
            return KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
                cluster,
                client_id=ManagedIdentityClientId
            )

        elif auth_method == "interactive":
            return KustoConnectionStringBuilder.with_interactive_login(cluster)

        elif auth_method == "azureCli":
            return KustoConnectionStringBuilder.with_az_cli_authentication(cluster)

        elif auth_method == "deviceCode":
            return KustoConnectionStringBuilder.with_aad_device_authentication(cluster)

        elif auth_method == "userToken":
            user_token = os.getenv(f"TEST_REPORT_AAD_USER_TOKEN{env_suffix}")
            if not user_token:
                raise RuntimeError(f"Could not load AAD User Token from environment{' (backup)' if backup else ''}")
            return KustoConnectionStringBuilder.with_aad_user_token_authentication(cluster, user_token)

        elif auth_method == "appToken":
            app_token = os.getenv(f"TEST_REPORT_AAD_APP_TOKEN{env_suffix}")
            if not app_token:
                raise RuntimeError(f"Could not load AAD App Token from environment{' (backup)' if backup else ''}")
            return KustoConnectionStringBuilder.with_aad_application_token_authentication(cluster, app_token)

        elif auth_method == "defaultCred":
            if DefaultAzureCredential is None:
                raise RuntimeError(
                    "azure-identity package is required for defaultCredential authentication method. "
                    "Install it with: pip install azure-identity"
                )
            try:
                credential = DefaultAzureCredential()
                # Try the correct method name - it might be with_aad_token_credential_authentication
                try:
                    return KustoConnectionStringBuilder.with_aad_token_credential_authentication(cluster, credential)
                except AttributeError:
                    # Fallback to other possible method names
                    return KustoConnectionStringBuilder.with_azure_token_credential(cluster, credential)
            except Exception as e:
                print(f"Failed to create DefaultAzureCredential: {e}")
                raise

        else:
            raise ValueError(f"Unsupported authentication method: {auth_method}")

    def upload_report(self, report_json: Dict,
                      external_tracking_id: str = "",
                      report_guid: str = "",
                      testbed: str = "",
                      os_version: str = "") -> None:
        """Upload a report to the back-end data store.

        Args:
            report_json: A JUnit test report in JSON format. See junit_xml_parser.
            external_tracking_id: An identifier that a client can use to map a test report
                to some external system of their choosing (e.g. Jenkins, Travis CI, JIRA, etc.).
                This id does not have to be unique.
            report_guid: A randomly generated UUID that is used to query for a specific test run across tables.
        """
        if not report_json:
            print(
                "Test result file is not found or empty. We will only upload pipeline results and summary.")
            self._upload_pipeline_results(
                external_tracking_id, report_guid, testbed, os_version)
            self._upload_summary(report_json, report_guid)
            return
        self._upload_pipeline_results(
            external_tracking_id, report_guid, testbed, os_version)
        self._upload_metadata(report_json, external_tracking_id, report_guid)
        self._upload_summary(report_json, report_guid)
        self._upload_test_cases(report_json, report_guid)

    def upload_reachability_data(self, ping_output: List) -> None:
        ping_time = str(datetime.utcnow())
        for result in ping_output:
            result.update({"UTCTimestamp": ping_time})
        self._ingest_data(self.TESTBEDREACHABILITY_TABLE, ping_output)

    def upload_swss_report_file(self, swss_file: str) -> None:
        """Upload a report to the back-end data store.
        Args:
            swss_file: json_file
        """
        self._upload_swss_log_file(swss_file)

    def upload_case_invoc_report_file(self, file) -> None:
        """Upload a report to the back-end data store.
        Args:
            file: json
        """
        self._upload_case_invoc_report_file(file)

    def upload_sai_header_def_report_file(self, file) -> None:
        """Upload a report to the back-end data store.
        Args:
            file: json
        """
        self._upload_sai_header_def_report_file(file)

    def upload_pdu_status_data(self, pdu_status_output: List) -> None:
        time = str(datetime.utcnow())
        pdu_output = []
        for result in pdu_status_output:
            if not result["PDU status"]:
                status = {"Timestamp": time,
                          "Host": result["Host"], "data_present": False}
                pdu_output.append(status)
                continue

            for status in result["PDU status"]:
                status.update(
                    {"Timestamp": time, "Host": result["Host"], "data_present": True})
                pdu_output.append(status)

        pdu_status_data = {"data": pdu_output}
        self._ingest_data(self.RAW_PDU_STATUS_TABLE, pdu_status_data)

    def upload_reboot_report(self, path_name: str = "", tracking_id: str = "", report_guid: str = "") -> None:
        reboot_timing_data = {
            "id": report_guid,
            "tracking_id": tracking_id
        }
        reboot_timing_dict = validate_json_file(path_name)
        reboot_timing_data.update(reboot_timing_dict)
        print("Uploading {} report with contents: {}".format(
            path_name, reboot_timing_data))
        if "summary.json" in path_name:
            self._ingest_data(self.REBOOT_TIMING_TABLE, reboot_timing_data)
        elif "report.json" in path_name:
            self._ingest_data(self.RAW_REBOOT_TIMING_TABLE, reboot_timing_data)

    def upload_expected_runs(self, expected_runs: List) -> None:
        self._ingest_data(self.EXPECTED_TEST_RUNS_TABLE, expected_runs)

    def upload_case_numbers(self, case_numbers: List) -> None:
        self._ingest_data(self.TEST_CASE_NUMBERS_TABLE, case_numbers)

    def _upload_swss_log_file(self, swss_file: str) -> None:
        self._ingest_data_file(self.SWSSDATA_TABLE, swss_file)

    def _upload_case_invoc_report_file(self, case_invoc_file):
        self._ingest_data_file(self.CASE_INVOC_TABLE, case_invoc_file)

    def _upload_sai_header_def_report_file(self, sai_header_def_file):
        self._ingest_data_file(self.SAI_HEADER_INVOC_TABLE, sai_header_def_file)

    def _upload_pipeline_results(self, external_tracking_id, report_guid, testbed, os_version):
        pipeline_data = {
            "id": report_guid,
            "tracking_id": external_tracking_id,
            "testbed": testbed,
            "os_version": os_version,
            "upload_time": str(datetime.utcnow())
        }
        try:
            # load pipeline task result json file
            with open(TASK_RESULT_FILE, 'r') as f:
                task_results = json.load(f)
        except Exception as e:
            print("Failed to load file {} with exception {}".format(
                TASK_RESULT_FILE, repr(e)))
            task_results = {}
        pipeline_data.update(task_results)
        print("Upload pipeline result")
        self._ingest_data(self.PIPELINE_TABLE, pipeline_data)

    def _upload_metadata(self, report_json, external_tracking_id, report_guid):
        metadata = {
            "id": report_guid,
            "tracking_id": external_tracking_id,
            "upload_time": str(datetime.utcnow())
        }
        metadata.update(report_json["test_metadata"])
        print("Upload metadata")
        self._ingest_data(self.METADATA_TABLE, metadata)

    def _upload_summary(self, report_json, report_guid):
        summary = {
            "id": report_guid
        }
        if not report_json:
            report_json = {
                "time": 0.0,
                "tests": 0,
                "skipped": 0,
                "failures": 0,
                "errors": 0,
                "xfails": 0
            }
            summary.update(report_json)
        else:
            summary.update(report_json["test_summary"])
        print("Upload summary")
        self._ingest_data(self.SUMMARY_TABLE, summary)

    def _upload_test_cases(self, report_json, report_guid):
        test_cases = []
        for feature, cases in report_json["test_cases"].items():
            for case in cases:
                case.update({
                    "id": report_guid,
                    "feature": feature
                })
                test_cases.append(case)
        print("Upload test case")
        self._ingest_data(self.TEST_CASE_TABLE, test_cases)

    def _ingest_data(self, table, data):
        props = IngestionProperties(
            database=self.db_name,
            table=table,
            data_format=self.TABLE_FORMAT_LOOKUP[table],
            ingestion_mapping_reference=self.TABLE_MAPPING_LOOKUP[table]
        )

        # Create temporary file with delete=False to avoid Windows permission issues
        temp_fd, temp_path = tempfile.mkstemp(suffix='.json', text=True)
        try:
            with os.fdopen(temp_fd, 'w') as temp_file:
                if isinstance(data, list):
                    temp_file.write('\n'.join([json.dumps(entry) for entry in data]))
                else:
                    temp_file.write(json.dumps(data))

            print("Ingest to primary cluster...")
            self._ingestion_client.ingest_from_file(
                temp_path, ingestion_properties=props)

            if self._ingestion_client_backup:
                print("Ingest to backup cluster...")
                self._ingestion_client_backup.ingest_from_file(
                    temp_path, ingestion_properties=props)

        except Exception as e:
            print(f"Ingestion failed with error: {e}")
            raise
        finally:
            # Clean up the temporary file
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception as cleanup_e:
                print(f"Warning - failed to clean up temp file {temp_path}: {cleanup_e}")

    def _ingest_data_file(self, table, data_file):
        props = IngestionProperties(
            database=self.db_name,
            table=table,
            data_format=self.TABLE_FORMAT_LOOKUP[table],
            ingestion_mapping_reference=self.TABLE_MAPPING_LOOKUP[table],
            flush_immediately=True
        )

        self._ingestion_client.ingest_from_file(
            data_file, ingestion_properties=props)
