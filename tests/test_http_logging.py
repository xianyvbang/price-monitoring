from app.main import _safe_request_payload


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
