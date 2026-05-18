from app.security import decrypt_value, encrypt_value


def test_encrypt_value_is_not_plaintext():
    encrypted = encrypt_value("secret-token", "test-key")

    assert encrypted != "secret-token"
    assert decrypt_value(encrypted, "test-key") == "secret-token"
