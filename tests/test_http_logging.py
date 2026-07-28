from app.main import _safe_request_payload, _without_redundant_case_aliases


class DummyRequest:
    query_params = {}
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "authorization": "Bearer secret",
    }


def test_request_logging_masks_sensitive_form_fields():
    body = (
        b"current_password=old-password&new_password=new-password&"
        b"confirm_password=new-password&key_id=group-1&api_key=api-secret&"
        b"email=user@example.com&name=visible"
    )

    logged = _safe_request_payload(DummyRequest(), body)

    assert "old-password" not in logged
    assert "new-password" not in logged
    assert "group-1" not in logged
    assert "api-secret" not in logged
    assert "user@example.com" not in logged
    assert "Bearer secret" not in logged
    assert "visible" in logged


def test_api_response_alias_cleanup_is_recursive_and_preserves_unique_keys():
    payload = {
        "site_url": "https://example.com",
        "siteUrl": "https://example.com",
        "newApi": [
            {
                "has_api_key": True,
                "hasApiKey": True,
                "camelOnly": "kept",
            }
        ],
    }

    assert _without_redundant_case_aliases(payload) == {
        "site_url": "https://example.com",
        "newApi": [{"has_api_key": True, "camelOnly": "kept"}],
    }
