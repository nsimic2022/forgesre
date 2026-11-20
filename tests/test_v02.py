from discovery import classify



def test_classify_roles():
    assert classify([22, 9100]) == "Possible Linux server"
    assert classify([9182]) == "Possible Windows server"
    assert classify([22, 9182]) == "Possible Windows server"
    assert classify([9100, 9182]) == "Possible Linux server"
    assert classify([9100, 9182], exporter_kind="windows") == "Possible Windows server"
    assert classify([161]) == "Possible network device"
    assert classify([443]) == "Possible web/appliance"
    assert classify([]) == "No open ports"


