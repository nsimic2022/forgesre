from investigation import DISCLAIMER, investigate


def test_heuristic_cpu_rca():
    result = investigate(
        {
            "incident": {"title": "High CPU", "asset": "forge-demo-01"},
            "asset": {"hostname": "forge-demo-01"},
            "alert": {"alertname": "HighCPU"},
            "metrics": {"cpu_percent": 94, "disk_percent": 20},
            "logs": ["cpu raised"],
            "history": [],
        }
    )
    assert result["disclaimer"] == DISCLAIMER
    assert 10 <= result["confidence"] <= 95
    assert "forge-demo-01" in result["summary"]
    assert result["provider"] == "builtin-analyst"


