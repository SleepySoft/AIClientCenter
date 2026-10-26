from AIClientCenter.factory_engine import builtin_factories
from AIClientCenter.factory_metadata import (
    configuration_metadata, set_factory_metadata,
)


def test_builtin_factory_metadata_inherits_delegate_parameters():
    metadata = configuration_metadata(builtin_factories())

    assert metadata["version"] == 1
    assert metadata["common_fields"]["availability"]
    assert metadata["factories"]["openai_compatible"]["fields"]["api_base_url"][
        "required"] is True
    assert "models" in metadata["factories"]["gemini_free"]["fields"]
    assert "mode" in metadata["factories"]["codex"]["fields"]
    assert "gateway_url" in metadata["factories"]["openclaw"]["fields"]


def test_custom_factory_can_publish_extra_parameter_help():
    def custom_factory(*, name, group, endpoint="local", **kwargs):
        return None

    set_factory_metadata(
        custom_factory,
        description="Custom test factory.",
        fields={"endpoint": "Custom endpoint.", "custom_flag": "Custom flag."},
    )
    metadata = configuration_metadata({"custom": custom_factory})["factories"][
        "custom"]

    assert metadata["description"] == "Custom test factory."
    assert metadata["fields"]["endpoint"]["description"] == "Custom endpoint."
    assert metadata["fields"]["custom_flag"]["description"] == "Custom flag."
