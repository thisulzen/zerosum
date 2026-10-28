from zerosum.crashtest import run


def test_killing_the_server_mid_load_loses_and_doubles_nothing(tmp_path):
    report = run(str(tmp_path), transfers=400, clients=8, kills=3)
    assert report.failures == []
    assert report.kills == 3
    assert report.accepted + report.refused == 400
    # If no request was ever cut off, the kills missed and this proved nothing.
    assert report.retried > 0
