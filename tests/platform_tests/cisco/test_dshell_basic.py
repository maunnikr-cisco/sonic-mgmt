"""
Tests for the Cisco platform debug shell in SONiC
"""
import time
import logging
import pytest
from tests.common.helpers.assertions import pytest_assert
from tests.common.cisco_data import is_cisco_device
pytestmark = [
    pytest.mark.sanity_check(skip_sanity=True),
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]


def check_config_flags(duthost):
    """
    @summary: This function reads and returns the values of the autostart and autorestart flags from the 
              debug shell client configuration that determine if debug shell will automatically be started and restarted
    """
    if not is_cisco_device(duthost):
        return
    config_file = duthost.command("docker exec syncd cat /etc/supervisor/conf.d/dshell_client.conf")["stdout"].split("\n")
    config_flags = {
        "autostart":    True,
        "autorestart":  True
    }
    for line in config_file:
        if "autostart" in line and "true" not in line:
            config_flags["autostart"] = False
        elif "autorestart" in line and "true" not in line:
            config_flags["autorestart"] = False
    return config_flags

def test_dshell_default_enabled(duthosts, enum_rand_one_per_hwsku_hostname):
    """
    @summary: Verify that the dshell client config flags have both been set to true, 
              and dshell client is enabled by default
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    if not is_cisco_device(duthost):
        pytest.skip("Testcase only supported on Cisco platforms")
    config_flags = check_config_flags(duthost)
    assert config_flags["autostart"], "autostart flag is set to False"
    assert config_flags["autorestart"], "autorestart flag is set to False"
    logging.info("dshell client has been enabled by default")
