from discovery import classify, hosts_from_cidrs



def test_classify_roles():
    assert classify([22, 9100]) == "Possible Linux server"
    assert classify([9182]) == "Possible Windows server"
    assert classify([22, 9182]) == "Possible Windows server"
    assert classify([9100, 9182]) == "Possible Linux server"
    assert classify([9100, 9182], exporter_kind="windows") == "Possible Windows server"
    assert classify([161]) == "Possible network device"
    assert classify([443]) == "Possible web/appliance"
    assert classify([]) == "No open ports"


def test_hosts_from_cidrs_skips_loopback_and_caps():
    assert hosts_from_cidrs(["127.0.0.0/8"]) == []
    assert hosts_from_cidrs(["not-a-cidr", "10.20.30.41/32"]) == ["10.20.30.41"]
    hosts = hosts_from_cidrs(["10.0.0.0/16"])
    assert len(hosts) == 256
    assert "10.0.0.1" in hosts


