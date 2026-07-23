"""
Test suite for dingtalk_api.py module
Tests: get_access_token, get_departments, get_department_users, get_all_users, token caching
"""
import asyncio
import time
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dingtalk_api import DingTalkClient, DINGTALK_BASE_URL


class TestDingTalkClientInit:
    """Test DingTalkClient initialization"""

    def test_init_stores_credentials(self):
        client = DingTalkClient("app_key_123", "app_secret_456")
        assert client.app_key == "app_key_123"
        assert client.app_secret == "app_secret_456"

    def test_init_creates_http_client(self):
        client = DingTalkClient("key", "secret")
        assert client.client is not None
        assert isinstance(client.client, httpx.AsyncClient)

    def test_init_token_state(self):
        client = DingTalkClient("key", "secret")
        assert client.access_token is None
        assert client.token_expires == 0


class TestGetAccessToken:
    """Test get_access_token method"""

    @pytest.mark.asyncio
    async def test_get_token_success(self):
        """Should successfully get access token from API"""
        client = DingTalkClient("key", "secret")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 0,
            "access_token": "token_abc123",
            "expires_in": 7200
        }

        with patch.object(client.client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            token = await client.get_access_token()

            assert token == "token_abc123"
            assert client.access_token == "token_abc123"
            assert client.token_expires > time.time()

            # Verify correct URL and params
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "gettoken" in call_args.args[0]
            assert call_args.kwargs["params"]["appkey"] == "key"
            assert call_args.kwargs["params"]["appsecret"] == "secret"

    @pytest.mark.asyncio
    async def test_get_token_api_error(self):
        """Should raise exception when API returns error"""
        client = DingTalkClient("key", "secret")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 40001,
            "errmsg": "invalid appkey"
        }

        with patch.object(client.client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            with pytest.raises(Exception, match="获取钉钉access_token失败"):
                await client.get_access_token()

    @pytest.mark.asyncio
    async def test_token_caching(self):
        """Token should be cached and not re-fetched if still valid"""
        client = DingTalkClient("key", "secret")
        client.access_token = "cached_token"
        client.token_expires = time.time() + 7200  # valid for 2 more hours

        with patch.object(client.client, "get", new_callable=AsyncMock) as mock_get:
            token = await client.get_access_token()

            assert token == "cached_token"
            mock_get.assert_not_called()  # Should not make API call

    @pytest.mark.asyncio
    async def test_token_refresh_before_expiry(self):
        """Token should be refreshed 5 minutes before expiry"""
        client = DingTalkClient("key", "secret")
        client.access_token = "old_token"
        # Set expiry to 4 minutes from now (within 5 min refresh window)
        client.token_expires = time.time() + 240

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 0,
            "access_token": "new_token",
            "expires_in": 7200
        }

        with patch.object(client.client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            token = await client.get_access_token()

            assert token == "new_token"
            mock_get.assert_called_once()  # Should have refreshed

    @pytest.mark.asyncio
    async def test_token_default_expiry(self):
        """If expires_in not in response, should default to 7200"""
        client = DingTalkClient("key", "secret")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 0,
            "access_token": "token_xyz",
            # No expires_in field
        }

        with patch.object(client.client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            await client.get_access_token()

            # Should have set expiry to now + 7200
            assert client.token_expires > time.time() + 7100


class TestGetDepartments:
    """Test get_departments and _get_sub_departments methods"""

    @pytest.mark.asyncio
    async def test_get_departments_recursive(self):
        """get_departments should recursively get all sub-departments"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        # Mock responses for recursive calls
        # Root (id=1) -> [Dept A (id=2), Dept B (id=3)]
        # Dept A (id=2) -> [Dept C (id=4)]
        # Dept B (id=3) -> []
        # Dept C (id=4) -> []
        responses = [
            # Root call
            MagicMock(json=MagicMock(return_value={
                "errcode": 0,
                "result": [
                    {"dept_id": 2, "name": "Dept A", "parent_id": 1},
                    {"dept_id": 3, "name": "Dept B", "parent_id": 1},
                ]
            })),
            # Dept A sub-departments
            MagicMock(json=MagicMock(return_value={
                "errcode": 0,
                "result": [
                    {"dept_id": 4, "name": "Dept C", "parent_id": 2},
                ]
            })),
            # Dept B sub-departments (empty)
            MagicMock(json=MagicMock(return_value={
                "errcode": 0,
                "result": []
            })),
            # Dept C sub-departments (empty)
            MagicMock(json=MagicMock(return_value={
                "errcode": 0,
                "result": []
            })),
        ]

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = responses

            departments = await client.get_departments()

            # Should have Dept A, Dept B, Dept C (root is not included)
            assert len(departments) == 3
            dept_ids = [d["dept_id"] for d in departments]
            assert 2 in dept_ids
            assert 3 in dept_ids
            assert 4 in dept_ids

    @pytest.mark.asyncio
    async def test_get_departments_api_error(self):
        """If API returns error for a department, should skip and continue"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 60011,
            "errmsg": "no permission"
        }

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            departments = await client.get_departments()
            assert len(departments) == 0  # No departments returned

    @pytest.mark.asyncio
    async def test_get_departments_dept_info_fields(self):
        """Department info should contain expected fields"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 0,
            "result": [
                {"dept_id": 2, "name": "Engineering", "parent_id": 1, "create_dept_group": True, "auto_add_user": True},
            ]
        }

        empty_response = MagicMock()
        empty_response.json.return_value = {"errcode": 0, "result": []}

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [mock_response, empty_response]

            departments = await client.get_departments()

            assert len(departments) == 1
            dept = departments[0]
            assert dept["dept_id"] == 2
            assert dept["name"] == "Engineering"
            assert dept["parent_id"] == 1
            assert dept["create_dept_group"] is True
            assert dept["auto_add_user"] is True


class TestGetDepartmentUsers:
    """Test get_department_users method with pagination"""

    @pytest.mark.asyncio
    async def test_get_department_users_single_page(self):
        """Should get users from a single page"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 0,
            "result": {
                "list": [
                    {"userid": "u1", "name": "张三", "mobile": "13800138000", "email": "zs@test.com", "title": "工程师", "job_number": "E001", "dept_id_list": [1], "active": True},
                ],
                "has_more": False
            }
        }

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            users = await client.get_department_users(1)

            assert len(users) == 1
            assert users[0]["userid"] == "u1"
            assert users[0]["name"] == "张三"
            assert users[0]["mobile"] == "13800138000"

    @pytest.mark.asyncio
    async def test_get_department_users_pagination(self):
        """Should handle pagination correctly"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        # Page 1: has_more=True, next_cursor=100
        page1 = MagicMock()
        page1.json.return_value = {
            "errcode": 0,
            "result": {
                "list": [
                    {"userid": "u1", "name": "User1", "dept_id_list": [1]},
                    {"userid": "u2", "name": "User2", "dept_id_list": [1]},
                ],
                "has_more": True,
                "next_cursor": 100
            }
        }

        # Page 2: has_more=False
        page2 = MagicMock()
        page2.json.return_value = {
            "errcode": 0,
            "result": {
                "list": [
                    {"userid": "u3", "name": "User3", "dept_id_list": [1]},
                ],
                "has_more": False
            }
        }

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [page1, page2]

            users = await client.get_department_users(1)

            assert len(users) == 3
            assert users[0]["userid"] == "u1"
            assert users[2]["userid"] == "u3"

            # Verify cursor was updated
            assert mock_post.call_count == 2
            second_call_body = mock_post.call_args_list[1].kwargs["json"]
            assert second_call_body["cursor"] == 100

    @pytest.mark.asyncio
    async def test_get_department_users_api_error(self):
        """API error should return empty list (break)"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "errcode": 60020,
            "errmsg": "department not found"
        }

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            users = await client.get_department_users(999)
            assert users == []


class TestGetAllUsers:
    """Test get_all_users method with deduplication"""

    @pytest.mark.asyncio
    async def test_get_all_users_dedup(self):
        """Users in multiple departments should be deduplicated"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        # Mock get_departments to return two departments
        dept_list_response = MagicMock()
        dept_list_response.json.return_value = {
            "errcode": 0,
            "result": [
                {"dept_id": 2, "name": "DeptA", "parent_id": 1},
                {"dept_id": 3, "name": "DeptB", "parent_id": 1},
            ]
        }
        empty_dept_response = MagicMock()
        empty_dept_response.json.return_value = {"errcode": 0, "result": []}

        # Mock get_department_users
        dept_a_users = MagicMock()
        dept_a_users.json.return_value = {
            "errcode": 0,
            "result": {
                "list": [
                    {"userid": "u1", "name": "张三", "dept_id_list": [2, 3]},
                    {"userid": "u2", "name": "李四", "dept_id_list": [2]},
                ],
                "has_more": False
            }
        }

        dept_b_users = MagicMock()
        dept_b_users.json.return_value = {
            "errcode": 0,
            "result": {
                "list": [
                    {"userid": "u1", "name": "张三", "dept_id_list": [2, 3]},  # duplicate
                    {"userid": "u3", "name": "王五", "dept_id_list": [3]},
                ],
                "has_more": False
            }
        }

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            # Order: dept list (root), dept A sub (empty), dept B sub (empty),
            # then users for dept 2, users for dept 3
            mock_post.side_effect = [
                dept_list_response,  # root dept list
                empty_dept_response,  # DeptA sub-depts (empty)
                empty_dept_response,  # DeptB sub-depts (empty)
                dept_a_users,  # users in dept 2
                dept_b_users,  # users in dept 3
            ]

            users = await client.get_all_users()

            # Should be 3 unique users (u1, u2, u3)
            assert len(users) == 3
            user_ids = [u["userid"] for u in users]
            assert "u1" in user_ids
            assert "u2" in user_ids
            assert "u3" in user_ids

    @pytest.mark.asyncio
    async def test_get_all_users_empty_userid_skipped(self):
        """Users with empty userid should be skipped"""
        client = DingTalkClient("key", "secret")
        client.access_token = "test_token"
        client.token_expires = time.time() + 7200

        dept_list_response = MagicMock()
        dept_list_response.json.return_value = {"errcode": 0, "result": []}

        with patch.object(client.client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = dept_list_response

            users = await client.get_all_users()
            assert users == []


class TestClose:
    """Test close method"""

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self):
        """close should call client.aclose()"""
        client = DingTalkClient("key", "secret")

        with patch.object(client.client, "aclose", new_callable=AsyncMock) as mock_aclose:
            await client.close()
            mock_aclose.assert_called_once()
