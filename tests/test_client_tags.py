"""Client 能力/敏感性标签（tags）与按标签过滤调度的测试。"""
from AIClientCenter.core.manager import AIClientManager
from AIClientCenter.tests.test_client_manager_health import FakeClient


class TaggedClient(FakeClient):
    """FakeClient 硬编码 name，这里允许指定名字、分组与标签。"""

    def __init__(self, name, group_id="default", tags=()):
        super().__init__()
        self.name = name
        self.group_id = group_id
        self.tags = frozenset(tags)


def _manager_with_tagged_clients():
    manager = AIClientManager()
    manager.register_client(TaggedClient("strict"))
    manager.register_client(TaggedClient("lax", tags={"low_sensitivity"}))
    return manager


def test_required_tags_filters_candidates():
    manager = _manager_with_tagged_clients()

    picked = manager.wait_for_available_client(
        "user", timeout=1, required_tags={"low_sensitivity"})
    assert picked is not None and picked.name == "lax"
    manager.release_client(picked)


def test_without_required_tags_any_client_may_serve():
    manager = _manager_with_tagged_clients()
    picked = manager.wait_for_available_client("user", timeout=1)
    assert picked is not None
    manager.release_client(picked)


def test_unknown_tag_matches_nothing():
    manager = _manager_with_tagged_clients()
    picked = manager.wait_for_available_client(
        "user", timeout=0.2, required_tags={"nonexistent_tag"})
    assert picked is None


def test_factory_engine_attaches_tags(tmp_path):
    import yaml
    from AIClientCenter.factory_engine import load_config

    config = {
        "clients": [{
            "factory": "mock_factory",
            "name": "tagged",
            "group": "g",
            "tags": ["low_sensitivity", "strong"],
        }],
    }
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    def mock_factory(name, group, **kwargs):
        assert "tags" not in kwargs  # tags 不传给工厂
        return TaggedClient(name, group_id=group)

    clients, _ = load_config(path, factories={"mock_factory": mock_factory})
    assert clients["tagged"].tags == frozenset({"low_sensitivity", "strong"})
