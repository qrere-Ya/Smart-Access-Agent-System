"""驗證流程共用工具：敏感資訊過濾、回答判讀（全形、中英文）、Console 的略過與結束、Markdown 表格與原子寫檔。"""
import pytest

from shared import verify_common as vc


def test_scrub_hides_known_secrets_long_tokens_and_digests_but_keeps_normal_text():
    token = "abcdEFGH1234" * 4                                   # 48 字元，像通行碼
    digest = "0123456789abcdef" * 4                              # 64 位元十六進位，像指紋或雜湊
    text = f"通行碼 {token} 指紋 {digest} 路徑 runtime/verification/2026-11-13_0830 姓名 葉君緯"
    out = vc.scrub(text)
    assert token not in out and digest not in out and out.count(vc.HIDDEN) == 2
    assert "2026-11-13_0830" in out and "葉君緯" in out
    assert vc.scrub("短短的祕密 s3cr3t!!", secrets=["s3cr3t!!"]) == "短短的祕密 " + vc.HIDDEN
    assert vc.scrub("abc", secrets=["", None, "ab"]) == "abc"      # 空字串與太短的不處理（避免把一般字蓋掉）


def test_parse_answer_accepts_fullwidth_case_and_chinese():
    assert vc.parse_answer("y") == ("y", "")
    assert vc.parse_answer("Ｙ 很清楚") == ("y", "很清楚")
    assert vc.parse_answer("  N  太小聲 ") == ("n", "太小聲")
    assert vc.parse_answer("是")[0] == "y" and vc.parse_answer("否 破音")[0] == "n"
    assert vc.parse_answer("對，很清楚")[0] == "y" and vc.parse_answer("不行")[0] == "n"
    assert vc.parse_answer("S")[0] == "s" and vc.parse_answer("ｑ")[0] == "q"
    assert vc.parse_answer("") == (None, "") and vc.parse_answer("亂打")[0] is None


def make_console(answers):
    said, queue = [], list(answers)

    def reader(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return vc.Console(reader, said.append), said


def test_console_skip_quit_and_eof():
    console, _ = make_console(["s"])
    with pytest.raises(vc.SkipStep):
        console.ask("?")
    console, _ = make_console(["Q"])
    with pytest.raises(vc.QuitSession):
        console.ask("?")
    console, _ = make_console([])
    with pytest.raises(vc.QuitSession):                          # 輸入來源結束當作 q
        console.pause()


def test_yes_no_retries_then_gives_up_with_none():
    console, said = make_console(["亂打", "y 清楚"])
    assert console.yes_no("好嗎") == (True, "清楚") and any("看不懂" in s for s in said)
    console, _ = make_console(["x", "x", "x"])
    assert console.yes_no("好嗎") == (None, "x")
    console, _ = make_console(["否 破音"])
    assert console.yes_no("好嗎") == (False, "破音")


def test_md_table_escapes_pipes_and_newlines():
    table = vc.md_table(["a", "b"], [("x|y", "第一行\n第二行"), (1, 2)])
    lines = table.splitlines()
    assert lines[0] == "| a | b |" and lines[1] == "|---|---|"
    assert "x\\|y" in lines[2] and "<br>" in lines[2] and len(lines) == 4


def test_atomic_write_replaces_the_file_and_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "sub" / "report.md")
    vc.atomic_write(path, "第一版")
    vc.atomic_write(path, "第二版")
    assert open(path, encoding="utf-8").read() == "第二版"
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["report.md"]
