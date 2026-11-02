from pathlib import Path

from bootstrap import verify

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"


def test_verify_reports_bad_key_file_as_error_code(tmp_path, capsys):
    (tmp_path / "prod-9999a.pub.json").write_text("{}")

    status = verify.main(
        [str(TESTDATA / "valid.jws"), "--env", "production", "--keys", str(tmp_path)]
    )

    assert status == 1
    assert capsys.readouterr().err.startswith("error: bad_key: prod-9999a.pub.json")


def test_verify_reports_missing_file_as_unreadable_input(tmp_path, capsys):
    status = verify.main([str(tmp_path / "absent.jws"), "--env", "production"])

    assert status == 1
    assert capsys.readouterr().err.startswith("error: unreadable_input: ")
