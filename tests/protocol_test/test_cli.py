from hilrig.protocol_test.cli import build_parser


def test_reset_reconnect_cli_exposes_explicit_unobserved_reset_fallback() -> None:
    args = build_parser().parse_args(
        ["reset-reconnect", "--port", "fake", "--allow-unobserved-reset"]
    )
    assert args.allow_unobserved_reset is True
