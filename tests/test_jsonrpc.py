from mcp_guard.jsonrpc import (
    blocked_response,
    encode_message,
    extract_tool_call,
    is_tools_call,
    parse_message,
    timeout_response,
)


def test_parse_and_encode_roundtrip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    raw = encode_message(msg)
    assert raw.endswith(b"\n")
    assert parse_message(raw) == msg


def test_is_tools_call_and_extract():
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "hi"}},
    }
    assert is_tools_call(msg)
    name, args = extract_tool_call(msg)
    assert name == "echo"
    assert args == {"message": "hi"}


def test_blocked_and_timeout_errors():
    blocked = blocked_response(3)
    assert blocked["error"]["code"] == -32042
    assert blocked["id"] == 3
    timed = timeout_response(4)
    assert timed["error"]["code"] == -32044
