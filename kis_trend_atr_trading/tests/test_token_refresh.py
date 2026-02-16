from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from api.kis_api import KISApi, KISApiError
from utils.market_hours import KST


@patch("api.kis_api.requests.post")
def test_access_token_refresh_called_when_expiry_is_near(mock_post, tmp_path):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "refreshed_token",
        "expires_in": 86400,
    }
    mock_post.return_value = mock_response

    with patch("api.kis_api.settings.DATA_DIR", tmp_path):
        with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
            api = KISApi(
                app_key="app-key",
                app_secret="app-secret",
                account_no="12345678",
                is_paper_trading=True,
            )
            api.access_token = "old_token"
            api.token_expires_at = datetime.now(KST) + timedelta(minutes=20)

            token = api.get_access_token()

    assert token == "refreshed_token"
    assert mock_post.call_count == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_error_refresh_once_then_retry(status_code):
    api = KISApi(
        app_key="app-key",
        app_secret="app-secret",
        account_no="12345678",
        is_paper_trading=True,
    )
    api.access_token = "old_token"
    api.token_expires_at = datetime.now(KST) + timedelta(hours=1)

    first_response = Mock()
    first_response.status_code = status_code
    first_response.ok = False
    first_response.text = "auth error"

    second_response = Mock()
    second_response.status_code = 200
    second_response.ok = True
    second_response.text = "ok"

    refresh_calls = []

    def _refresh(force_refresh: bool = False):
        refresh_calls.append(force_refresh)
        api.access_token = "new_token"
        return "new_token"

    with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
        with patch("api.kis_api.time.sleep", return_value=None):
            with patch("api.kis_api.requests.get", side_effect=[first_response, second_response]) as mock_get:
                with patch.object(api, "get_access_token", side_effect=_refresh):
                    headers = {"authorization": "Bearer old_token"}
                    response = api._request_with_retry(
                        method="GET",
                        url="https://example.com/mock",
                        headers=headers,
                        max_retries=1,
                    )

    assert response.status_code == 200
    assert refresh_calls == [True]
    assert mock_get.call_count == 2
    second_headers = mock_get.call_args_list[1].kwargs["headers"]
    assert second_headers["authorization"] == "Bearer new_token"


@patch("api.kis_api.requests.post")
def test_missing_credentials_blocks_external_token_request(mock_post, tmp_path):
    with patch("api.kis_api.settings.APP_KEY", ""):
        with patch("api.kis_api.settings.APP_SECRET", ""):
            with patch("api.kis_api.settings.DATA_DIR", tmp_path):
                api = KISApi(
                    app_key="",
                    app_secret="",
                    account_no="12345678",
                    is_paper_trading=True,
                )
                with pytest.raises(KISApiError) as exc_info:
                    api.get_access_token(force_refresh=True)

    assert "KIS_APP_KEY/KIS_APP_SECRET" in str(exc_info.value)
    mock_post.assert_not_called()
