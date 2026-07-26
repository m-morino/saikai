"""Headless protocol tests for the incremental VT tokenizer.

Run:  python tests/test_terminal_protocol.py
"""
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_terminal as rt


def _terminal(cols=40, rows=8):
    """Real pyte-backed terminal with query writes captured synchronously."""
    import pyte

    terminal = rt.AgentTerminal(
        ["agent"], status_classifier=lambda _text, _title: "idle")
    terminal._screen = rt._HistoryScreenBase(cols, rows, history=20)
    terminal._stream = pyte.Stream(terminal._screen)
    terminal._sync_output = rt._SynchronizedOutputStager()
    terminal._protocol_replies = []
    terminal._send_to_child = terminal._protocol_replies.append
    terminal._marshal = lambda fn: fn()
    return terminal


def _screen_text(terminal):
    return "\n".join(rt._pyte_grid_lines(terminal._screen))


def _tokens_after_byte_split(data: bytes, split_at: int):
    """Feed latin-1 decoded bytes in two calls, retaining C1 codepoints exactly."""
    tokenizer = rt.VTTokenizer()
    return tokenizer.feed(data[:split_at].decode("latin-1")) + tokenizer.feed(
        data[split_at:].decode("latin-1"))


def _one_shot_tokens(data: bytes):
    return rt.VTTokenizer().feed(data.decode("latin-1"))


def test_complete_sequences_match_at_every_byte_boundary():
    """A PTY split cannot change the recognized raw VT token stream."""
    cases = {
        "CSI": b"\x1b[31;1m",
        "DECRQM": b"\x1b[?2026$p",
        "DECSCUSR": b"\x1b[5 q",
        "OSC": b"\x1b]9;notice\x07",
        "DCS": b"\x1bP$qpayload\x1b\\",
        "APC": b"\x1b_payload\x1b\\",
        "PM": b"\x1b^payload\x1b\\",
        "SOS": b"\x1bXpayload\x1b\\",
        "simple ESC": b"\x1b7",
        "C1 CSI": b"\x9b?2026$p",
        "C1 OSC": b"\x9d9;notice\x9c",
        "C1 DCS": b"\x90$qpayload\x9c",
        "C1 APC": b"\x9fpayload\x9c",
        "C1 PM": b"\x9epayload\x9c",
        "C1 SOS": b"\x98payload\x9c",
    }
    for name, data in cases.items():
        expected = _one_shot_tokens(data)
        for split_at in range(len(data) + 1):
            actual = _tokens_after_byte_split(data, split_at)
            assert actual == expected, f"{name} split at byte {split_at}: {actual!r}"


def test_csi_keeps_parameter_intermediate_and_final_grammar():
    """Dropping CSI intermediates would misclassify DECRQM as a complete token."""
    tokens = rt.VTTokenizer().feed("\x1b[?2026$p")
    assert len(tokens) == 1
    token = tokens[0]
    assert token.kind == "csi"
    assert token.raw == "\x1b[?2026$p"
    assert token.parameters == "?2026"
    assert token.intermediates == "$"
    assert token.final == "p"


def test_simple_escape_keeps_exact_raw_data():
    """DECSC's `7` is an ESC final byte, not printable text after an ESC."""
    for split_at in range(3):
        tokens = _tokens_after_byte_split(b"\x1b7", split_at)
        assert [(token.kind, token.raw) for token in tokens] == [("esc", "\x1b7")]


def test_ordinary_text_and_c0_controls_keep_exact_raw_data():
    """Status consumers need raw control data preserved rather than normalized."""
    tokens = rt.VTTokenizer().feed("plain\r\ntext\x07")
    assert [(token.kind, token.raw) for token in tokens] == [
        ("text", "plain"), ("control", "\r"), ("control", "\n"),
        ("text", "text"), ("control", "\x07"),
    ]


def test_c0_inside_csi_and_escape_executes_in_order_without_ending_sequence():
    """C0 format effectors execute immediately while the enclosing VT state continues."""
    for control in ("\x07", "\x08", "\x09", "\x0a", "\x0b", "\x0c", "\x0d"):
        cases = (
            (
                ("A\x1b[3" + control + "1mB").encode("latin-1"),
                [
                    ("text", "A", "", "", ""),
                    ("control", control, "", "", ""),
                    ("csi", "\x1b[31m", "31", "", "m"),
                    ("text", "B", "", "", ""),
                ],
            ),
            (
                ("A\x1b[5 " + control + "qB").encode("latin-1"),
                [
                    ("text", "A", "", "", ""),
                    ("control", control, "", "", ""),
                    ("csi", "\x1b[5 q", "5", " ", "q"),
                    ("text", "B", "", "", ""),
                ],
            ),
            (
                ("A\x1b(" + control + "BB").encode("latin-1"),
                [
                    ("text", "A", "", "", ""),
                    ("control", control, "", "", ""),
                    ("esc", "\x1b(B", "", "(", "B"),
                    ("text", "B", "", "", ""),
                ],
            ),
        )
        for raw, expected in cases:
            one_shot = _one_shot_tokens(raw)
            assert [
                (token.kind, token.raw, token.parameters,
                 token.intermediates, token.final)
                for token in one_shot
            ] == expected, (repr(control), repr(raw), one_shot)
            for split_at in range(len(raw) + 1):
                assert _tokens_after_byte_split(raw, split_at) == one_shot, (
                    repr(control), repr(raw), split_at
                )


def test_can_sub_and_escape_keep_cancelling_active_csi_and_escape():
    """Ordered C0 support must not turn the three existing cancellers into continuations."""
    for prefix in ("\x1b[31", "\x1b("):
        for cancel in ("\x18", "\x1a"):
            tokens = rt.VTTokenizer().feed(prefix + cancel + "X")
            assert [(token.kind, token.raw, token.literal) for token in tokens] == [
                ("text", prefix, True),
                ("control", cancel, False),
                ("text", "X", False),
            ]

    tokens = rt.VTTokenizer().feed("\x1b[31\x1b[0mX")
    assert [(token.kind, token.raw, token.literal) for token in tokens] == [
        ("text", "\x1b[31", True),
        ("csi", "\x1b[0m", False),
        ("text", "X", False),
    ]
    tokens = rt.VTTokenizer().feed("\x1b(\x1b7X")
    assert [(token.kind, token.raw, token.literal) for token in tokens] == [
        ("text", "\x1b(", True),
        ("esc", "\x1b7", False),
        ("text", "X", False),
    ]


def test_osc_recognizes_bel_and_st_terminators_for_supported_codes():
    """A terminator regression would leave title, clipboard, and notifications carried."""
    for code in ("9", "52", "777", "99"):
        for terminator in ("\x07", "\x1b\\", "\x9c"):
            raw = f"\x1b]{code};payload{terminator}"
            tokens = rt.VTTokenizer().feed(raw)
            assert [(token.kind, token.raw) for token in tokens] == [("osc", raw)]


def test_dcs_payload_does_not_create_nested_osc_or_csi_tokens():
    """DCS payload is opaque until ST (or defensive BEL)."""
    raw = "\x1bPpayload-[not-a-vt]-end\x1b\\"
    tokens = rt.VTTokenizer().feed(raw)
    assert [(token.kind, token.raw) for token in tokens] == [("dcs", raw)]
    bel_raw = "\x1bPpayload\x07"
    assert [(token.kind, token.raw) for token in rt.VTTokenizer().feed(bel_raw)] == [
        ("dcs", bel_raw)
    ]


def test_apc_pm_sos_payloads_are_opaque_until_st_not_bel():
    """APC/PM/SOS are strings too; BEL remains payload while ST ends them."""
    for kind, opener in (("apc", "\x1b_"), ("pm", "\x1b^"),
                         ("sos", "\x1bX"), ("apc", "\x9f"),
                         ("pm", "\x9e"), ("sos", "\x98")):
        raw = opener + "payload\x07[not-a-vt]\x1b\\"
        assert [(token.kind, token.raw) for token in rt.VTTokenizer().feed(raw)] == [
            (kind, raw)
        ]


def test_control_strings_abort_on_can_sub_and_new_control_introducers():
    """Cancelled strings suppress their side effects and reparse the interrupt."""
    for opener in ("\x1b]", "\x1bP", "\x1b_", "\x1b^", "\x1bX",
                   "\x9d", "\x90", "\x9f", "\x9e", "\x98"):
        for abort in ("\x18", "\x1a"):
            raw = opener + "payload" + abort + "B"
            tokens = rt.VTTokenizer().feed(raw)
            assert [(token.kind, token.raw) for token in tokens] == [
                ("ignored", opener + "payload" + abort), ("text", "B")
            ], (repr(opener), repr(abort), tokens)

    # ESC starts a new sequence rather than staying black-holed inside OSC.
    raw = b"\x1b]52;c;Y2xpcA==\x1b[31mB"
    expected = _one_shot_tokens(raw)
    assert [(token.kind, token.raw) for token in expected] == [
        ("ignored", "\x1b]52;c;Y2xpcA=="), ("csi", "\x1b[31m"),
        ("text", "B"),
    ]
    for split_at in range(len(raw) + 1):
        assert _tokens_after_byte_split(raw, split_at) == expected, split_at

    c1_raw = b"\x9dpayload\x9b31mB"
    assert [(token.kind, token.raw) for token in _one_shot_tokens(c1_raw)] == [
        ("ignored", "\x9dpayload"), ("csi", "\x9b31m"), ("text", "B"),
    ]


def test_cancelled_osc52_never_reaches_side_effects_or_grid():
    """An OSC 52 cancelled by CAN/SUB must not copy its partial payload."""
    for abort in ("\x18", "\x1a"):
        raw = "A\x1b]52;c;Y2xpcGJvYXJk" + abort + "B"
        for split_at in range(len(raw) + 1):
            terminal = _terminal(cols=60, rows=3)
            copied = []
            terminal._honor_osc52 = copied.append
            terminal._consume(raw[:split_at])
            terminal._consume(raw[split_at:])
            assert copied == [], (repr(abort), split_at, copied)
            assert _screen_text(terminal).strip() == "AB", (repr(abort), split_at)


def test_incomplete_sequences_carry_across_calls_and_fail_open_when_bounded():
    """An unfinished stream must retain only a bounded prefix and eventually emit it."""
    tokenizer = rt.VTTokenizer(max_carry=16, max_dropped_string=24)
    assert tokenizer.feed("\x1b[?2026$") == []
    assert tokenizer.carry == "\x1b[?2026$"
    assert [(token.kind, token.raw) for token in tokenizer.feed("p")] == [
        ("csi", "\x1b[?2026$p")
    ]

    for opener, filler in (("\x1b]", "x"), ("\x1bP", "x"), ("\x1b[", "?")):
        bounded = rt.VTTokenizer(max_carry=16, max_dropped_string=24)
        emitted = bounded.feed(opener + filler * 200)
        assert len(bounded.carry) <= 16
        assert bounded.dropped_string_chars <= 24
        assert emitted, f"{opener!r} stayed buffered after the carry cap"


def test_completed_oversize_sequences_fail_open_at_every_split_boundary():
    """An over-cap VT unit stays text whether its terminator shares a PTY read."""
    cases = {
        "CSI": b"\x1b[" + b"?" * 50 + b"p",
        "simple ESC": b"\x1b" + b" " * 50 + b"7",
        "OSC": b"\x1b]" + b"x" * 50 + b"\x07",
        "DCS": b"\x1bP" + b"x" * 50 + b"\x1b\\",
    }
    for name, data in cases.items():
        raw = data.decode("latin-1")
        one_shot = rt.VTTokenizer(max_carry=16, max_dropped_string=24)
        assert [(token.kind, token.raw) for token in one_shot.feed(raw)] == [
            ("text", raw)
        ], name
        for split_at in range(len(data) + 1):
            tokenizer = rt.VTTokenizer(max_carry=16, max_dropped_string=24)
            tokens = tokenizer.feed(data[:split_at].decode("latin-1"))
            tokens += tokenizer.feed(data[split_at:].decode("latin-1"))
            assert all(token.kind == "text" for token in tokens), \
                f"{name} split at {split_at}: {tokens!r}"
            assert "".join(token.raw for token in tokens) == raw
            assert len(tokenizer.carry) <= 16
            assert tokenizer.dropped_string_chars <= 24
            assert [(token.kind, token.raw) for token in tokenizer.feed("\x1b[31m")] == [
                ("csi", "\x1b[31m")
            ], f"{name} split at {split_at} left fail-open state behind"


def test_consume_dispatches_mixed_queries_in_stream_order_without_collapsing():
    """Each request gets one position-correct reply in request order."""
    terminal = _terminal()
    terminal._consume(
        "\x1b[3;7H\x1b[6n"
        "\x1b[c"
        "\x1b[5n"
        "\x1b[0c"
    )
    assert "".join(terminal._protocol_replies) == (
        "\x1b[3;7R"
        "\x1b[?62;22c"
        "\x1b[0n"
        "\x1b[?62;22c"
    )


def test_private_dsr5_is_not_answered_or_stripped_as_a_saikai_query():
    """Only standard 5n and 6n plus DECXCPR ?6n belong to saikai."""
    terminal = _terminal()
    terminal._consume("A\x1b[?5nB\x1b[5n\x1b[?6n")
    assert "".join(terminal._protocol_replies) == "\x1b[0n\x1b[?1;2R"
    assert rt._MIRROR_QUERY_STRIP_RE.search("\x1b[?5n") is None
    assert rt._MIRROR_QUERY_STRIP_RE.search("\x1b[5n") is not None
    assert rt._MIRROR_QUERY_STRIP_RE.search("\x1b[?6n") is not None


def test_decrqm_observes_set_then_reset_at_each_stream_position():
    """A chunk-final pre-scan must not answer both DECRQMs from final mode state."""
    tracked = ("1", "25", "47", "1047", "1049", "1000", "1002", "1003",
               "1004", "1006", "2004", "2026")
    for mode in tracked:
        terminal = _terminal()
        terminal._consume(
            f"\x1b[?{mode}h"
            f"\x1b[?{mode}$p"
            f"\x1b[?{mode}l"
            f"\x1b[?{mode}$p"
        )
        assert "".join(terminal._protocol_replies) == (
            f"\x1b[?{mode};1$y\x1b[?{mode};2$y"
        ), mode
        assert terminal._sync_output.active is False, mode


def test_combined_dec_private_lists_apply_every_parameter():
    """DECSET/DECRST parameters are sequential mode operations, not one number."""
    terminal = _terminal()
    terminal._consume("\x1b[?2004;1004h")
    assert terminal._bracketed_paste is True
    assert terminal._focus_reporting is True

    terminal._consume("\x1b[?1049;25h")
    terminal._consume("\x1b[?1049$p\x1b[?25$p")
    assert "".join(terminal._protocol_replies[-2:]) == (
        "\x1b[?1049;1$y\x1b[?25;1$y"
    )
    assert terminal._alt.in_alt is True

    terminal._consume("\x1b[?1049;25l")
    terminal._consume("\x1b[?1049$p\x1b[?25$p")
    assert "".join(terminal._protocol_replies[-2:]) == (
        "\x1b[?1049;2$y\x1b[?25;2$y"
    )
    assert terminal._alt.in_alt is False


def test_split_decrqm_uses_tokenizer_carry_and_never_opens_sync_staging():
    """Every split of CSI ? 2026 $ p is one query, never a false DECSET."""
    query = "\x1b[?2026$p"
    for split_at in range(len(query) + 1):
        terminal = _terminal()
        terminal._consume(query[:split_at])
        terminal._consume(query[split_at:])
        assert "".join(terminal._protocol_replies) == "\x1b[?2026;2$y", split_at
        assert terminal._sync_output.active is False, split_at


def test_da_xtversion_and_repeated_queries_report_only_real_capabilities():
    """DA omits unsupported graphics/editing claims and repeats are not folded."""
    terminal = _terminal()
    terminal._consume(
        "\x1b[c\x1b[c"
        "\x1b[>0q"
        "\x1b[5n\x1b[5n"
        "\x1b]11;?\x07\x1b]11;?\x07"
    )
    replies = "".join(terminal._protocol_replies)
    assert replies.count("\x1b[?62;22c") == 2
    first_da = replies.split("c", 1)[0].removeprefix("\x1b[?")
    da_codes = set(first_da.split(";"))
    assert not da_codes.intersection({"4", "6", "8", "28"})
    assert "\x1bP>|saikai\x1b\\" in replies
    assert replies.count("\x1b[0n") == 2
    assert replies.count("\x1b]11;rgb:1e1e/1e1e/1e1e\x07") == 2


def test_kitty_keyboard_query_set_modes_mask_unsupported_flags():
    """Kitty replace/set/reset mutations report only flags saikai can emit."""
    terminal = _terminal()
    terminal._consume(
        "\x1b[?u"             # initial query
        "\x1b[=1u\x1b[?u"     # mode 1 (default): replace
        "\x1b[=8;2u\x1b[?u"   # mode 2: set bits
        "\x1b[=1;3u\x1b[?u"   # mode 3: reset bits
        "\x1b[=31u\x1b[?u"    # unsupported 2/4/16 are masked
    )
    assert "".join(terminal._protocol_replies) == (
        "\x1b[?0u"
        "\x1b[?1u"
        "\x1b[?1u"
        "\x1b[?0u"
        "\x1b[?1u"
    )
    assert "u" not in _screen_text(terminal).strip()


def test_kitty_capability_mask_does_not_claim_lost_textual_key_identity():
    """Do not advertise report-all after Textual has collapsed keypad identity."""
    from textual._keyboard_protocol import FUNCTIONAL_KEYS

    # These distinct Kitty inputs reach AgentTerminal as the same public Key
    # names as their non-keypad counterparts. encode_key cannot reconstruct
    # which physical key was pressed.
    assert FUNCTIONAL_KEYS["57399u"] == "0"
    assert FUNCTIONAL_KEYS["57414u"] == "enter"
    assert FUNCTIONAL_KEYS["57417u"] == "left"

    assert rt._KITTY_KBD_SUPPORTED_FLAGS == 1
    terminal = _terminal()
    terminal._consume("\x1b[=8u\x1b[?u\x1b[=9u\x1b[?u")
    assert "".join(terminal._protocol_replies) == "\x1b[?0u\x1b[?1u"


def test_kitty_keyboard_push_pop_is_bounded_and_main_alt_separate():
    """Push/pop stacks are bounded and scoped independently per screen buffer."""
    terminal = _terminal()
    terminal._consume("\x1b[=1u\x1b[>0u\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?0u"

    terminal._consume("\x1b[?1049h\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?0u"
    terminal._consume("\x1b[=1u\x1b[>0u\x1b[<u\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?1u"

    terminal._consume("\x1b[?1049l\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?0u"
    terminal._consume("\x1b[<u\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?1u"
    terminal._consume("\x1b[<2u\x1b[?u")
    assert terminal._protocol_replies[-1] == "\x1b[?0u"

    for flag in range(rt._KITTY_KBD_STACK_MAX + 20):
        terminal._consume(f"\x1b[>{flag & 1}u")
    stack = terminal._kitty_keyboard_stacks[False]
    assert len(stack) == rt._KITTY_KBD_STACK_MAX


def test_tokenized_dcs_and_fail_open_controls_cannot_trigger_side_effects():
    """Opaque/oversize controls stay inert; fail-open bytes become visible data."""
    terminal = _terminal(cols=120, rows=4)
    copied = []
    terminal._honor_osc52 = copied.append

    terminal._consume(
        "A\x1bPpayload]52;c;Y2xpcGJvYXJk"
        "[?2004h[c\x1b\\B"
    )
    assert copied == []
    assert terminal._bracketed_paste is False
    assert terminal._protocol_replies == []
    assert _screen_text(terminal).strip() == "AB"

    terminal._vt_tokenizer = rt.VTTokenizer(max_carry=16, max_dropped_string=24)
    oversize = "\x1b]52;c;" + ("YQ==" * 20) + "\x07"
    terminal._consume(oversize)
    assert copied == []
    visible = _screen_text(terminal)
    assert "52;c;" in visible
    assert "YQ==" in visible


def test_dependency_lists_have_runtime_parity():
    """Direct-script installation must include every runtime package dependency."""
    root = Path(__file__).parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_deps = project["project"]["dependencies"]
    header = (root / "saikai.py").read_text(encoding="utf-8").split("# ///", 2)[1]
    header_lines = [line.removeprefix("#").strip() for line in header.splitlines()]
    start = header_lines.index("dependencies = [") + 1
    pep_deps = []
    for line in header_lines[start:]:
        if line == "]":
            break
        pep_deps.append(line.split("#", 1)[0].strip().rstrip(",").strip('"'))
    assert pep_deps == project_deps
    for package in ("rich", "regex", "segno", "cryptography"):
        assert any(dependency.startswith(package) for dependency in project_deps), \
            f"runtime dependency missing {package}"
    assert "rich>=15.0.0" in project_deps, \
        "grapheme-aware cell_len/load_cell_table requires Rich 15 or newer"


if __name__ == "__main__":
    test_complete_sequences_match_at_every_byte_boundary()
    print("PASS test_complete_sequences_match_at_every_byte_boundary")
    test_csi_keeps_parameter_intermediate_and_final_grammar()
    print("PASS test_csi_keeps_parameter_intermediate_and_final_grammar")
    test_simple_escape_keeps_exact_raw_data()
    print("PASS test_simple_escape_keeps_exact_raw_data")
    test_ordinary_text_and_c0_controls_keep_exact_raw_data()
    print("PASS test_ordinary_text_and_c0_controls_keep_exact_raw_data")
    test_c0_inside_csi_and_escape_executes_in_order_without_ending_sequence()
    print("PASS test_c0_inside_csi_and_escape_executes_in_order_without_ending_sequence")
    test_can_sub_and_escape_keep_cancelling_active_csi_and_escape()
    print("PASS test_can_sub_and_escape_keep_cancelling_active_csi_and_escape")
    test_osc_recognizes_bel_and_st_terminators_for_supported_codes()
    print("PASS test_osc_recognizes_bel_and_st_terminators_for_supported_codes")
    test_dcs_payload_does_not_create_nested_osc_or_csi_tokens()
    print("PASS test_dcs_payload_does_not_create_nested_osc_or_csi_tokens")
    test_apc_pm_sos_payloads_are_opaque_until_st_not_bel()
    print("PASS test_apc_pm_sos_payloads_are_opaque_until_st_not_bel")
    test_control_strings_abort_on_can_sub_and_new_control_introducers()
    print("PASS test_control_strings_abort_on_can_sub_and_new_control_introducers")
    test_cancelled_osc52_never_reaches_side_effects_or_grid()
    print("PASS test_cancelled_osc52_never_reaches_side_effects_or_grid")
    test_incomplete_sequences_carry_across_calls_and_fail_open_when_bounded()
    print("PASS test_incomplete_sequences_carry_across_calls_and_fail_open_when_bounded")
    test_completed_oversize_sequences_fail_open_at_every_split_boundary()
    print("PASS test_completed_oversize_sequences_fail_open_at_every_split_boundary")
    test_consume_dispatches_mixed_queries_in_stream_order_without_collapsing()
    print("PASS test_consume_dispatches_mixed_queries_in_stream_order_without_collapsing")
    test_private_dsr5_is_not_answered_or_stripped_as_a_saikai_query()
    print("PASS test_private_dsr5_is_not_answered_or_stripped_as_a_saikai_query")
    test_decrqm_observes_set_then_reset_at_each_stream_position()
    print("PASS test_decrqm_observes_set_then_reset_at_each_stream_position")
    test_combined_dec_private_lists_apply_every_parameter()
    print("PASS test_combined_dec_private_lists_apply_every_parameter")
    test_split_decrqm_uses_tokenizer_carry_and_never_opens_sync_staging()
    print("PASS test_split_decrqm_uses_tokenizer_carry_and_never_opens_sync_staging")
    test_da_xtversion_and_repeated_queries_report_only_real_capabilities()
    print("PASS test_da_xtversion_and_repeated_queries_report_only_real_capabilities")
    test_kitty_keyboard_query_set_modes_mask_unsupported_flags()
    print("PASS test_kitty_keyboard_query_set_modes_mask_unsupported_flags")
    test_kitty_capability_mask_does_not_claim_lost_textual_key_identity()
    print("PASS test_kitty_capability_mask_does_not_claim_lost_textual_key_identity")
    test_kitty_keyboard_push_pop_is_bounded_and_main_alt_separate()
    print("PASS test_kitty_keyboard_push_pop_is_bounded_and_main_alt_separate")
    test_tokenized_dcs_and_fail_open_controls_cannot_trigger_side_effects()
    print("PASS test_tokenized_dcs_and_fail_open_controls_cannot_trigger_side_effects")
    test_dependency_lists_have_runtime_parity()
    print("PASS test_dependency_lists_have_runtime_parity")
