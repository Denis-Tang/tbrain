from tbrain import identity


class FakeIndex:
    def __init__(self, links=None):
        self.links = links or []
        self.registered = []

    def find_link_by_cwd_and_message(self, cwd, first_message_hash):
        for link in self.links:
            if link["cwd"] == cwd and link["first_message_hash"] == first_message_hash:
                return link["session_id"]
        return ""

    def register_link(self, host, host_session_id, session_id, cwd, first_message_hash, created_at):
        self.registered.append((host, host_session_id, session_id, cwd, first_message_hash, created_at))


def test_build_and_split_session_id():
    assert identity.build_session_id("claude-code", "abc") == "claude-code:abc"
    assert identity.split_session_id("claude-code:abc") == ("claude-code", "abc")
    assert identity.split_session_id("no-separator") is None
    assert identity.split_session_id("claude-code:") is None


def test_host_aliases_and_unknown():
    assert identity.normalize_host("Claude") == "claude-code"
    assert identity.normalize_host("DeepSeek-Harness") == "dsh"
    assert identity.normalize_host("oh-dsh") == "dsh"
    assert identity.normalize_host("weird") == "unknown"
    assert identity.build_session_id("Claude", "x") == "claude-code:x"


def test_first_message_hash_is_stable_and_whitespace_insensitive():
    left = identity.first_message_hash("hello\n")
    right = identity.first_message_hash("hello   ")
    assert left == right
    assert left != identity.first_message_hash("hello world")


def test_find_reused_session_uses_cwd_and_message():
    digest = identity.first_message_hash("帮我把索引改好")
    index = FakeIndex([{"cwd": r"D:\work", "first_message_hash": digest, "session_id": "claude-code:old"}])
    assert identity.find_reused_session(index, r"D:\work", "帮我把索引改好") == "claude-code:old"
    assert identity.find_reused_session(index, r"D:\work", "别的消息") == ""
    assert identity.find_reused_session(index, "", "帮我把索引改好") == ""


def test_register_records_link():
    index = FakeIndex()
    ident = identity.Identity(host="pi", host_session_id="h1")
    identity.register(index, ident, "pi:h1", "/tmp/x", "hello", "2026-10-24T00:00:00Z")
    assert index.registered[0][0] == "pi"
    assert index.registered[0][2] == "pi:h1"
