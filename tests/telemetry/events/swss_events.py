#! /usr/bin/env python3

import logging
import time
import random
import re

from run_events_test import run_test

random.seed(10)
logger = logging.getLogger(__name__)
tag = "sonic-events-swss"

PFC_STORM_TEST_QUEUE = "4"
PFC_STORM_DETECTION_TIME = 100
PFC_STORM_RESTORATION_TIME = 100
CRM_DEFAULT_POLLING_INTERVAL = 300
CRM_DEFAULT_ACL_GROUP_HIGH = 85
CRM_TEST_POLLING_INTERVAL = 1
CRM_TEST_ACL_GROUP_HIGH = 0
WAIT_TIME = 3


def test_event(duthost, gnxi_path, ptfhost, data_dir, validate_yang):
    logger.info("Beginning to test swss events")
    run_test(duthost, gnxi_path, ptfhost, data_dir, validate_yang, shutdown_interface,
             "if_state.json", "sonic-events-swss:if-state", tag)
    run_test(duthost, gnxi_path, ptfhost, data_dir, validate_yang, generate_pfc_storm,
             "pfc_storm.json", "sonic-events-swss:pfc-storm", tag)
    run_test(duthost, gnxi_path, ptfhost, data_dir, validate_yang, trigger_crm_threshold_exceeded,
             "chk_crm_threshold.json", "sonic-events-swss:chk_crm_threshold", tag)


def shutdown_interface(duthost):
    logger.info("Shutting down interface")
    interfaces = duthost.get_interfaces_status()
    pattern = re.compile(r'^Ethernet[0-9]{1,2}$')
    interface_list = []
    for interface, status in interfaces.items():
        if pattern.match(interface) and status["oper"] == "up" and status["admin"] == "up":
            interface_list.append(interface)
    if_state_test_port = random.choice(interface_list)
    assert if_state_test_port is not None, "Unable to find valid interface for test"

    ret = duthost.shell("config interface shutdown {}".format(if_state_test_port))
    assert ret["rc"] == 0, "Failing to shutdown interface {}".format(if_state_test_port)

    ret = duthost.shell("config interface startup {}".format(if_state_test_port))
    assert ret["rc"] == 0, "Failing to startup interface {}".format(if_state_test_port)


def generate_pfc_storm(duthost):
    logger.info("Generating pfc storm")
    interfaces = duthost.get_interfaces_status()
    pattern = re.compile(r'^Ethernet[0-9]{1,2}$')
    interface_list = []
    for interface, status in interfaces.items():
        if pattern.match(interface) and status["oper"] == "up" and status["admin"] == "up":
            interface_list.append(interface)
    PFC_STORM_TEST_PORT = random.choice(interface_list)
    assert PFC_STORM_TEST_PORT is not None, "Unable to find valid interface for test"

    queue_oid = duthost.get_queue_oid(PFC_STORM_TEST_PORT, PFC_STORM_TEST_QUEUE)
    duthost.shell("sonic-db-cli COUNTERS_DB HSET \"COUNTERS:{}\" \"DEBUG_STORM\" \"enabled\"".
                  format(queue_oid))
    duthost.shell("pfcwd start --action drop {} {} --restoration-time {}".
                  format(PFC_STORM_TEST_PORT, PFC_STORM_DETECTION_TIME, PFC_STORM_RESTORATION_TIME))
    time.sleep(WAIT_TIME)  # give time for pfcwd to detect pfc storm
    duthost.shell("pfcwd stop")
    duthost.shell("sonic-db-cli COUNTERS_DB HDEL \"COUNTERS:{}\" \"DEBUG_STORM\"".
                  format(queue_oid))


def trigger_crm_threshold_exceeded(duthost):
    logger.info("Triggering crm threshold exceeded")
    duthost.shell("crm config polling interval {}".format(CRM_TEST_POLLING_INTERVAL))
    duthost.shell("crm config thresholds acl group high {}".format(CRM_TEST_ACL_GROUP_HIGH))
    time.sleep(WAIT_TIME)  # give time for crm threshold exceed to be detected
    duthost.shell("crm config polling interval {}".format(CRM_DEFAULT_POLLING_INTERVAL))
    duthost.shell("crm config thresholds acl group high {}".format(CRM_DEFAULT_ACL_GROUP_HIGH))
