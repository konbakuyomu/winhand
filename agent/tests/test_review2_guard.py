"""Search-pattern false positives, refusal summaries and built-in login-file rules."""

import pytest

from winhand import actions, guard

NO_BATCH_DELETE = r"(?i)(\brm\s+(-[a-z]+\s+)*(-[a-z]*r[a-z]*|--recursive)\b|\b(remove-item|ri|rm|del|erase|rd|rmdir)\b[^\n;|&]*\s-(r|re|rec|recu|recur|recurs|recurse)\b|\b(rd|rmdir|del|erase)\b[^\n;|&]*\s/s\b|\b(remove-item|ri|rm|del|erase)\b[^\n;|&]*[*?]|\|\s*(remove-item|ri|rm|del|erase)\b|(\bforeach-object\b|\bforeach\b|%)\s*(\([^)]*\)\s*)?\{[^}]*\b(remove-item|ri|rm|del|erase)\b|\[(system\.)?io\.directory\]::delete\b|\brimraf\b|\bfs(\.promises)?\.(rm|rmdir)(sync)?\s*\([^\n]*recursive\s*:\s*true|\bxargs\s+rm\b|\bgit\s+clean\b|\bfind\b[^\n]*\s-delete\b|\brmtree\b)"


@pytest.fixture
def rules():
    return guard._parse(
        "guards.toml",
        None,
        (
            '[[guard]]\non = ["command", "input"]\ndecision = "deny"\n'
            f"match = '{NO_BATCH_DELETE}'\nreason = '禁止批量删除'\n"
        ).encode(),
    )


@pytest.mark.parametrize("kind", ["command", "input"])
@pytest.mark.parametrize(
    "text",
    [
        r"""Select-String learn\colab_lane.py -Pattern '"stop"|"download"|"delete"|\[\"(stop|terminate|close|rm)' | % { "$($_.LineNumber): $($_.Line)" }""",
        r"""Select-String -Path tools\t257_probe.py -Pattern 'probe"|out\b|exists|已有|mkdir|rmtree' | Select -First 12""",
        """sls -Pattern 'rmtree',"| rm" a.txt""",
        """Select-String -Pattern "rmtree" a.txt""",
        "rg -n 'rmtree' .",
        'grep -i "rmtree" a.txt',
        'findstr -i "rmtree" a.txt',
        "rg -n -e 'rmtree' .",
        "grep -e 'rmtree' a.txt",
    ],
)
def test_search_patterns_do_not_match_delete_rules(rules, kind, text):
    assert rules.error is None
    assert not guard.matches(rules.rules[0], actions.Action(kind, "run", text=text))


@pytest.mark.parametrize("kind", ["command", "input"])
@pytest.mark.parametrize(
    "text",
    [
        r'Remove-Item "$d\*.py"',
        "Get-ChildItem x | Remove-Item",
        "Remove-Item -Recurse x",
        r'foreach ($s in $a) { Remove-Item "$d\$s" }',
        "[IO.Directory]::Delete($p, $true)",
        "Select-String -Pattern 'x' a.txt; Remove-Item a*",
        "rg 'foo' . ; rm -rf build",
        '''python -c "import shutil; shutil.rmtree('x')"''',
        "rg --pre 'Remove-Item -Recurse x' 'pattern' .",
        "rg -n --pre='rmtree.bat' 'pattern' .",
        """grep "$(Remove-Item -Recurse x)" a.txt""",
    ],
)
def test_delete_commands_stay_blocked(rules, kind, text):
    assert guard.matches(rules.rules[0], actions.Action(kind, "run", text=text))


def test_only_pattern_contents_change():
    text = """Select-String -Path "a.txt" -Pattern 'rmtree',"| rm" | rg -e 'rm' .; Remove-Item a*"""
    expected = """Select-String -Path "a.txt" -Pattern 'xxxxxx',"xxxx" | rg -e 'xx' .; Remove-Item a*"""
    assert guard._command_view(text) == expected


def test_other_action_kinds_keep_the_original_text():
    rule = guard._rule("guards.toml", 1, {"on": "mcp", "match": "rmtree", "decision": "deny"}, None)
    assert guard.matches(rule, actions.Action("mcp", "mcp_call", text="rg 'rmtree' ."))


def test_refusal_first_line_names_the_first_denied_rule(rules):
    rule = rules.rules[0]
    rule.reason = "禁止批量删除" * 12
    action = actions.Action("command", "run", text="Remove-Item -Recurse x")
    second = guard._rule("other.toml", 2, {"on": "command", "decision": "deny", "reason": "other"}, None)
    verdict = guard.Verdict(deny=[(rule, action), (second, action)])
    assert guard.refusal(verdict).splitlines()[0] == (
        f"refused by a guard rule on this machine (guards.toml rule 1: {rule.reason[:60]})"
    )


@pytest.mark.parametrize(
    ("path", "denied"),
    [
        ("C:/tmp/p/.config/colab-cli/token.json", True),
        ("C:/tmp/p/.config/colab-cli/colab.log", False),
        ("C:/Users/u/.ssh/id_ed25519", True),
        ("C:/Users/u/.ssh/id_ed25519.pub", False),
    ],
)
def test_builtin_login_file_protection(path, denied):
    action = actions.Action("read", "fs_read", (path,))
    assert (
        any(rule.decision == "deny" and guard.matches(rule, action) for rule in guard.builtin_file().rules)
        == denied
    )
