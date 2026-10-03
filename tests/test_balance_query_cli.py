from AIClientCenter import AiServiceBalanceQuery


def test_balance_query_main_reads_explicit_key_file(monkeypatch, tmp_path):
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text("first-key\nsecond-key\n", encoding="utf-8")
    queried = []

    def fake_query(key):
        queried.append(key)
        return {"success": True, "data": {"platform": "siliconflow"}}

    monkeypatch.setattr(AiServiceBalanceQuery, "get_siliconflow_balance", fake_query)

    assert AiServiceBalanceQuery.main(str(keys_file)) == 2
    assert queried == ["first-key", "second-key"]


def test_balance_query_cli_requires_explicit_key_file():
    args = AiServiceBalanceQuery.parse_args(["--keys-file", "keys.txt"])

    assert args.keys_file == "keys.txt"
