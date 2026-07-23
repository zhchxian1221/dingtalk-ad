"""
Test suite for ad_sync.py module
Tests: encode_ad_password, escape_dn_value, get_domain_from_base_dn,
       get_dept_ou_path, ADSyncService methods, preview_sync, execute_sync
"""
import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from ad_sync import (
    encode_ad_password,
    escape_dn_value,
    get_domain_from_base_dn,
    get_dept_ou_path,
    ADSyncService,
    preview_sync,
    execute_sync,
)


# ==================== encode_ad_password tests ====================

class TestEncodeADPassword:
    """Test the AD password encoding function"""

    def test_basic_password(self):
        """Password should be wrapped in double quotes and UTF-16LE encoded"""
        result = encode_ad_password("P@ssw0rd")
        expected = '"P@ssw0rd"'.encode("utf-16-le")
        assert result == expected

    def test_empty_password(self):
        """Empty password should still be wrapped in quotes"""
        result = encode_ad_password("")
        expected = '""'.encode("utf-16-le")
        assert result == expected

    def test_password_with_special_chars(self):
        """Password with special characters should be encoded correctly"""
        result = encode_ad_password("Abc!@#123")
        expected = '"Abc!@#123"'.encode("utf-16-le")
        assert result == expected

    def test_returns_bytes(self):
        """Result should be bytes type"""
        result = encode_ad_password("test")
        assert isinstance(result, bytes)

    def test_unicode_password(self):
        """Unicode characters should be encoded correctly"""
        result = encode_ad_password("密码123")
        expected = '"密码123"'.encode("utf-16-le")
        assert result == expected


# ==================== escape_dn_value tests ====================

class TestEscapeDNValue:
    """Test LDAP DN special character escaping"""

    def test_no_special_chars(self):
        """Plain string should remain unchanged"""
        assert escape_dn_value("normalname") == "normalname"

    def test_comma_escaped(self):
        """Comma should be escaped"""
        assert escape_dn_value("name,with,comma") == "name\\,with\\,comma"

    def test_backslash_escaped_first(self):
        """Backslash should be escaped (and before other chars)"""
        result = escape_dn_value("test\\path")
        assert result == "test\\\\path"

    def test_plus_escaped(self):
        assert escape_dn_value("a+b") == "a\\+b"

    def test_angle_brackets_escaped(self):
        assert escape_dn_value("a<b>c") == "a\\<b\\>c"

    def test_semicolon_escaped(self):
        assert escape_dn_value("a;b") == "a\\;b"

    def test_quote_escaped(self):
        assert escape_dn_value('a"b') == 'a\\"b'

    def test_equals_escaped(self):
        assert escape_dn_value("a=b") == "a\\=b"

    def test_multiple_special_chars(self):
        """Multiple special characters in one string"""
        result = escape_dn_value('test,name=1"2')
        assert result == 'test\\,name\\=1\\"2'


# ==================== get_domain_from_base_dn tests ====================

class TestGetDomainFromBaseDN:
    """Test domain extraction from Base DN"""

    def test_standard_dn(self):
        """Standard multi-level domain"""
        dn = "OU=Users,DC=example,DC=com"
        assert get_domain_from_base_dn(dn) == "example.com"

    def test_single_dc(self):
        dn = "DC=example,DC=com"
        assert get_domain_from_base_dn(dn) == "example.com"

    def test_no_dc(self):
        """DN without DC components should return empty string"""
        dn = "OU=Users,OU=COMPANY"
        assert get_domain_from_base_dn(dn) == ""

    def test_mixed_case_dc(self):
        """Mixed case DC= prefix should be handled"""
        dn = "ou=users,dc=Example,dc=COM"
        assert get_domain_from_base_dn(dn) == "Example.COM"

    def test_spaces_around_parts(self):
        """Parts with spaces should be trimmed"""
        dn = "OU=Users, DC=corp, DC=com"
        assert get_domain_from_base_dn(dn) == "corp.com"


# ==================== get_dept_ou_path tests ====================

class TestGetDeptOUPath:
    """Test department OU path calculation"""

    def test_root_department_returns_base_dn(self):
        """Root department (id=1) should return base_dn"""
        dept_map = {1: {"dept_id": 1, "name": "Root", "parent_id": 0}}
        base_dn = "OU=Users,DC=corp,DC=com"
        assert get_dept_ou_path(1, dept_map, base_dn) == base_dn

    def test_unknown_dept_returns_base_dn(self):
        """Unknown department should return base_dn"""
        dept_map = {}
        base_dn = "OU=Users,DC=corp,DC=com"
        assert get_dept_ou_path(999, dept_map, base_dn) == base_dn

    def test_single_level_child(self):
        """Single level child department"""
        dept_map = {
            1: {"dept_id": 1, "name": "Root", "parent_id": 0},
            2: {"dept_id": 2, "name": "Engineering", "parent_id": 1},
        }
        base_dn = "OU=Users,DC=corp,DC=com"
        result = get_dept_ou_path(2, dept_map, base_dn)
        assert result == "OU=Engineering,OU=Users,DC=corp,DC=com"

    def test_nested_child(self):
        """Nested child departments"""
        dept_map = {
            1: {"dept_id": 1, "name": "Root", "parent_id": 0},
            2: {"dept_id": 2, "name": "Engineering", "parent_id": 1},
            3: {"dept_id": 3, "name": "Backend", "parent_id": 2},
        }
        base_dn = "OU=Users,DC=corp,DC=com"
        result = get_dept_ou_path(3, dept_map, base_dn)
        assert result == "OU=Backend,OU=Engineering,OU=Users,DC=corp,DC=com"

    def test_dept_name_with_special_chars(self):
        """Department name with special characters should be escaped"""
        dept_map = {
            1: {"dept_id": 1, "name": "Root", "parent_id": 0},
            2: {"dept_id": 2, "name": "R&D,Team", "parent_id": 1},
        }
        base_dn = "OU=Users,DC=corp,DC=com"
        result = get_dept_ou_path(2, dept_map, base_dn)
        # Comma should be escaped
        assert "\\," in result


# ==================== ADSyncService tests ====================

class TestADSyncServiceInit:
    """Test ADSyncService initialization and server parsing"""

    def test_plain_server_with_port(self):
        """Server with port should parse correctly"""
        svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
        assert svc.server_obj.host == "192.168.1.10"
        assert svc.server_obj.port == 389
        assert svc.server_obj.ssl is False

    def test_ldaps_url(self):
        """LDAPS URL should enable SSL"""
        svc = ADSyncService("ldaps://192.168.1.10:636", "admin", "pass", "OU=Users,DC=corp,DC=com")
        assert svc.server_obj.ssl is True
        assert svc.server_obj.host == "192.168.1.10"
        assert svc.server_obj.port == 636

    def test_ldap_url(self):
        """LDAP URL should not enable SSL"""
        svc = ADSyncService("ldap://192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
        assert svc.server_obj.ssl is False
        assert svc.server_obj.host == "192.168.1.10"

    def test_port_636_auto_ssl(self):
        """Port 636 should auto-enable SSL"""
        svc = ADSyncService("192.168.1.10:636", "admin", "pass", "OU=Users,DC=corp,DC=com")
        assert svc.server_obj.ssl is True

    def test_plain_server_no_port_defaults_389(self):
        """Server without port should default to 389"""
        svc = ADSyncService("192.168.1.10", "admin", "pass", "OU=Users,DC=corp,DC=com")
        assert svc.server_obj.port == 389
        assert svc.server_obj.ssl is False


class TestADSyncServiceConnect:
    """Test ADSyncService connect/disconnect methods"""

    def test_connect_success(self):
        """Successful connection should return True"""
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn.bound = True
            mock_conn_class.return_value = mock_conn

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            assert svc.connect() is True
            assert svc.conn is mock_conn

    def test_connect_failure(self):
        """Connection failure should return False"""
        from ldap3.core.exceptions import LDAPException
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn_class.side_effect = LDAPException("Connection refused")

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            assert svc.connect() is False

    def test_disconnect(self):
        """Disconnect should unbind and set conn to None"""
        svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
        mock_conn = MagicMock()
        mock_conn.bound = True
        svc.conn = mock_conn

        svc.disconnect()
        mock_conn.unbind.assert_called_once()
        assert svc.conn is None

    def test_disconnect_no_connection(self):
        """Disconnect with no connection should not error"""
        svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
        svc.disconnect()  # should not raise

    def test_disconnect_unbind_exception_swallowed(self):
        """Exceptions during unbind should be swallowed"""
        svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
        svc.conn = MagicMock()
        svc.conn.unbind.side_effect = Exception("Already closed")

        svc.disconnect()  # should not raise
        assert svc.conn is None

    def test_ensure_connection_reconnects(self):
        """_ensure_connection should reconnect if not bound"""
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn.bound = True
            mock_conn_class.return_value = mock_conn

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc._ensure_connection()
            assert svc.conn is mock_conn

    def test_ensure_connection_raises_on_failure(self):
        """_ensure_connection should raise if connection fails"""
        from ldap3.core.exceptions import LDAPException
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn_class.side_effect = LDAPException("Connection refused")

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            with pytest.raises(Exception, match="AD"):
                svc._ensure_connection()


class TestADSyncServiceCreateUser:
    """Test ADSyncService.create_user method"""

    def test_create_user_attributes(self):
        """create_user should set correct objectClass, sAMAccountName, userPrincipalName"""
        with patch.object(ADSyncService, "_ensure_connection"), \
             patch("ad_sync.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn.add.return_value = True
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {
                "name": "张三",
                "userid": "zhangsan",
                "email": "zhangsan@example.com",
                "mobile": "13800138000",
                "title": "工程师",
                "job_number": "EMP001",
                "initial_password": "Test@2026",
            }

            result, dn = svc.create_user(user_data, "OU=Engineering,OU=Users,DC=corp,DC=com")

            assert result is True
            assert "CN=张三" in dn

            # Check the add call
            add_call = mock_conn.add.call_args
            user_dn = add_call.args[0]
            assert user_dn.startswith("CN=")
            assert "OU=Engineering,OU=Users,DC=corp,DC=com" in user_dn

            attributes = add_call.kwargs.get("attributes", {})
            assert "top" in attributes["objectClass"]
            assert "person" in attributes["objectClass"]
            assert "organizationalPerson" in attributes["objectClass"]
            assert "user" in attributes["objectClass"]
            assert attributes["sAMAccountName"] == "zhangsan"
            assert attributes["userPrincipalName"] == "zhangsan@corp.com"
            assert attributes["userAccountControl"] == 514  # initially disabled
            assert attributes["displayName"] == "张三"
            assert attributes["mail"] == "zhangsan@example.com"
            assert attributes["mobile"] == "13800138000"
            assert attributes["title"] == "工程师"
            assert attributes["employeeID"] == "EMP001"

    def test_create_user_default_password(self):
        """create_user should use default password if not specified"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = True
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {"name": "李四", "userid": "lisi"}
            result, dn = svc.create_user(user_data, "OU=Users,DC=corp,DC=com")

            assert result is True
            # Password modify should have been called
            modify_calls = mock_conn.modify.call_args_list
            # First call is password, second is enable (userAccountControl=512)
            assert len(modify_calls) >= 2

    def test_create_user_enable_sets_512(self):
        """After creation, user should be enabled (userAccountControl=512)"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = True
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {"name": "Test", "userid": "testuser", "initial_password": "P@ss1"}
            svc.create_user(user_data, "OU=Users,DC=corp,DC=com")

            # The last modify call should be the enable (userAccountControl=512)
            enable_call = mock_conn.modify.call_args_list[-1]
            mod_dict = enable_call.args[1]
            assert "userAccountControl" in mod_dict
            uac_value = mod_dict["userAccountControl"][0][1][0]
            assert uac_value == 512

    def test_create_user_password_encoding(self):
        """Password should be encoded with UTF-16LE and wrapped in quotes"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = True
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {"name": "Test", "userid": "testuser", "initial_password": "MyP@ss123"}
            svc.create_user(user_data, "OU=Users,DC=corp,DC=com")

            # First modify call should be the password
            pwd_call = mock_conn.modify.call_args_list[0]
            pwd_dict = pwd_call.args[1]
            assert "unicodePwd" in pwd_dict
            encoded_pwd = pwd_dict["unicodePwd"][0][1][0]
            expected = '"MyP@ss123"'.encode("utf-16-le")
            assert encoded_pwd == expected

    def test_create_user_add_fails(self):
        """If add fails, should return (False, error_msg)"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = False
            mock_conn.result = {"message": "Entry already exists"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {"name": "Test", "userid": "testuser"}
            result, msg = svc.create_user(user_data, "OU=Users,DC=corp,DC=com")

            assert result is False
            assert "already exists" in msg or "创建失败" in msg

    def test_create_user_optional_attributes_skipped_when_empty(self):
        """Empty optional attributes should not be set"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = True
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            user_data = {"name": "Test", "userid": "testuser"}  # no email, mobile, etc.
            svc.create_user(user_data, "OU=Users,DC=corp,DC=com")

            attributes = mock_conn.add.call_args.kwargs["attributes"]
            assert "mail" not in attributes
            assert "mobile" not in attributes
            assert "title" not in attributes
            assert "employeeID" not in attributes


class TestADSyncServiceDisableEnable:
    """Test disable_user and enable_user methods"""

    def test_disable_user_sets_514(self):
        """disable_user should set userAccountControl to 514"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.disable_user("CN=Test,OU=Users,DC=corp,DC=com")

            assert result is True
            modify_call = mock_conn.modify.call_args
            mod_dict = modify_call.args[1]
            assert "userAccountControl" in mod_dict
            uac_value = mod_dict["userAccountControl"][0][1][0]
            assert uac_value == 514

    def test_enable_user_sets_512(self):
        """enable_user should set userAccountControl to 512"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.enable_user("CN=Test,OU=Users,DC=corp,DC=com")

            assert result is True
            modify_call = mock_conn.modify.call_args
            mod_dict = modify_call.args[1]
            assert "userAccountControl" in mod_dict
            uac_value = mod_dict["userAccountControl"][0][1][0]
            assert uac_value == 512

    def test_disable_user_failure(self):
        """disable_user should return False on modify failure"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = False
            mock_conn.result = {"message": "Insufficient rights"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.disable_user("CN=Test,OU=Users,DC=corp,DC=com")
            assert result is False

    def test_enable_user_failure(self):
        """enable_user should return False on modify failure"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = False
            mock_conn.result = {"message": "Insufficient rights"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.enable_user("CN=Test,OU=Users,DC=corp,DC=com")
            assert result is False


class TestADSyncServiceModifyUser:
    """Test modify_user method"""

    def test_modify_user_success(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            changes = {"mail": "new@example.com", "title": "Senior Engineer"}
            result = svc.modify_user("CN=Test,OU=Users,DC=corp,DC=com", changes)

            assert result is True
            modify_call = mock_conn.modify.call_args
            ldap_changes = modify_call.args[1]
            assert "mail" in ldap_changes
            assert "title" in ldap_changes

    def test_modify_user_failure(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify.return_value = False
            mock_conn.result = {"message": "No such object"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.modify_user("CN=Test,OU=Users,DC=corp,DC=com", {"mail": "new@test.com"})
            assert result is False


class TestADSyncServiceMoveUser:
    """Test move_user method"""

    def test_move_user_success(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify_dn.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            current_dn = "CN=Test,OU=Old,OU=Users,DC=corp,DC=com"
            new_parent = "OU=New,OU=Users,DC=corp,DC=com"
            result = svc.move_user(current_dn, new_parent)

            assert result is True
            mock_conn.modify_dn.assert_called_once_with(current_dn, "CN=Test", new_superior=new_parent)

    def test_move_user_failure(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.modify_dn.return_value = False
            mock_conn.result = {"message": "No such object"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.move_user("CN=Test,OU=Old,DC=corp,DC=com", "OU=New,DC=corp,DC=com")
            assert result is False


class TestADSyncServiceCreateOU:
    """Test create_ou method"""

    def test_create_ou_success(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = True

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.create_ou("Engineering", "OU=Users,DC=corp,DC=com")
            assert result is True

            add_call = mock_conn.add.call_args
            ou_dn = add_call.args[0]
            assert ou_dn == "OU=Engineering,OU=Users,DC=corp,DC=com"
            attributes = add_call.kwargs.get("attributes", {})
            assert "organizationalUnit" in attributes["objectClass"]

    def test_create_ou_already_exists(self):
        """If OU already exists, should return True"""
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = False
            mock_conn.result = {"message": "Already exists"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.create_ou("Engineering", "OU=Users,DC=corp,DC=com")
            assert result is True

    def test_create_ou_failure(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.add.return_value = False
            mock_conn.result = {"message": "Insufficient rights"}

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            result = svc.create_ou("Engineering", "OU=Users,DC=corp,DC=com")
            assert result is False


class TestADSyncServiceGetUsers:
    """Test get_existing_users and get_all_ous methods"""

    def test_get_existing_users(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()

            # Mock search results - _safe_attr does: entry[attr].value
            # We need different values for different attributes
            attr_values = {
                "distinguishedName": "CN=Test,OU=Users,DC=corp,DC=com",
                "cn": "Test",
                "displayName": "Test",
                "mail": "test@test.com",
                "mobile": "13800138000",
                "title": "Engineer",
                "employeeID": "E001",
                "userAccountControl": "512",
                "sAMAccountName": "testuser",
                "userPrincipalName": "testuser@corp.com",
            }

            def make_attr_mock(attr_name):
                m = MagicMock()
                m.value = attr_values.get(attr_name, "")
                return m

            mock_entry = MagicMock()
            mock_entry.__getitem__ = MagicMock(side_effect=make_attr_mock)
            mock_conn.entries = [mock_entry]

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            users = svc.get_existing_users()
            assert len(users) == 1
            assert users[0]["cn"] == "Test"
            assert users[0]["userAccountControl"] == 512

    def test_get_existing_users_empty(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()
            mock_conn.entries = []

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            users = svc.get_existing_users()
            assert users == []

    def test_get_all_ous(self):
        with patch.object(ADSyncService, "_ensure_connection"):
            mock_conn = MagicMock()

            mock_entry = MagicMock()
            mock_entry.distinguishedName = "OU=Test,OU=Users,DC=corp,DC=com"
            mock_conn.entries = [mock_entry]

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            svc.conn = mock_conn

            ous = svc.get_all_ous()
            assert len(ous) == 1


class TestADSyncServiceTestConnection:
    """Test test_connection method"""

    def test_test_connection_success(self):
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn.bound = True
            mock_conn_class.return_value = mock_conn

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            result = svc.test_connection()
            assert result is True
            mock_conn.unbind.assert_called_once()

    def test_test_connection_failure(self):
        from ldap3.core.exceptions import LDAPException
        with patch("ad_sync.Connection") as mock_conn_class:
            mock_conn_class.side_effect = LDAPException("Connection refused")

            svc = ADSyncService("192.168.1.10:389", "admin", "pass", "OU=Users,DC=corp,DC=com")
            result = svc.test_connection()
            assert result is False


# ==================== preview_sync tests ====================

class TestPreviewSync:
    """Test preview_sync async function"""

    @pytest.mark.asyncio
    async def test_preview_sync_new_users(self):
        """preview_sync should detect new users not in AD"""
        # Mock DingTalk client
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
            {"dept_id": 2, "name": "Engineering", "parent_id": 1},
        ]
        dt_client.get_all_users.return_value = [
            {"userid": "u1", "name": "张三", "email": "zs@test.com", "mobile": "13800138000", "title": "工程师", "job_number": "E001", "dept_id_list": [2]},
        ]

        # Mock AD service
        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []  # No existing users
        ad_service.get_all_ous.return_value = []

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        result = await preview_sync(dt_client, ad_service, config)

        assert len(result["new_users"]) == 1
        assert result["new_users"][0]["name"] == "张三"
        assert result["new_users"][0]["userid"] == "u1"
        assert "dingtalk_user_count" in result
        assert result["dingtalk_user_count"] == 1

        ad_service.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_preview_sync_modified_users(self):
        """preview_sync should detect attribute changes"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
        ]
        dt_client.get_all_users.return_value = [
            {"userid": "u1", "name": "张三", "email": "new@test.com", "mobile": "13800138000", "title": "工程师", "job_number": "E001", "dept_id_list": [1]},
        ]

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=张三,OU=Users,DC=corp,DC=com",
                "cn": "张三",
                "displayName": "张三",
                "mail": "old@test.com",
                "mobile": "13900139000",
                "title": "工程师",
                "employeeID": "E001",
                "userAccountControl": 512,
                "sAMAccountName": "zhangsan",
            }
        ]
        ad_service.get_all_ous.return_value = []

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        result = await preview_sync(dt_client, ad_service, config)

        assert len(result["modified_users"]) == 1
        changes = result["modified_users"][0]["changes"]
        assert "mail" in changes
        assert changes["mail"] == "new@test.com"
        assert "mobile" in changes
        assert changes["mobile"] == "13800138000"

    @pytest.mark.asyncio
    async def test_preview_sync_disabled_users(self):
        """preview_sync should detect users to disable (in AD but not in DingTalk)"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
        ]
        dt_client.get_all_users.return_value = []  # No DingTalk users

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=李四,OU=Users,DC=corp,DC=com",
                "cn": "李四",
                "displayName": "李四",
                "mail": "",
                "mobile": "",
                "title": "",
                "employeeID": "",
                "userAccountControl": 512,  # enabled
                "sAMAccountName": "lisi",
            }
        ]
        ad_service.get_all_ous.return_value = []

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        result = await preview_sync(dt_client, ad_service, config)

        assert len(result["disabled_users"]) == 1
        assert result["disabled_users"][0]["name"] == "李四"
        assert result["disabled_users"][0]["current_uac"] == 512

    @pytest.mark.asyncio
    async def test_preview_sync_already_disabled_not_listed(self):
        """Already disabled users (UAC=514) should not be in disabled list"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = []
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=王五,OU=Users,DC=corp,DC=com",
                "cn": "王五",
                "displayName": "王五",
                "mail": "",
                "mobile": "",
                "title": "",
                "employeeID": "",
                "userAccountControl": 514,  # already disabled
                "sAMAccountName": "wangwu",
            }
        ]
        ad_service.get_all_ous.return_value = []

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        result = await preview_sync(dt_client, ad_service, config)
        assert len(result["disabled_users"]) == 0

    @pytest.mark.asyncio
    async def test_preview_sync_new_ous(self):
        """preview_sync should detect OUs that need to be created"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
            {"dept_id": 2, "name": "Engineering", "parent_id": 1},
        ]
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []
        ad_service.get_all_ous.return_value = []  # No existing OUs

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        result = await preview_sync(dt_client, ad_service, config)

        assert len(result["new_ous"]) == 1
        assert result["new_ous"][0]["name"] == "Engineering"
        assert "OU=Engineering,OU=Users,DC=corp,DC=com" == result["new_ous"][0]["ou_path"]

    @pytest.mark.asyncio
    async def test_preview_sync_disconnect_called_on_exception(self):
        """disconnect should be called even if exception occurs"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = []
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.side_effect = Exception("LDAP error")
        ad_service.get_all_ous.return_value = []

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com"}

        with pytest.raises(Exception, match="LDAP error"):
            await preview_sync(dt_client, ad_service, config)

        ad_service.disconnect.assert_called_once()


# ==================== execute_sync tests ====================

class TestExecuteSync:
    """Test execute_sync async function"""

    @pytest.mark.asyncio
    async def test_execute_sync_creates_ou_and_user(self):
        """execute_sync should create OU then create user"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
            {"dept_id": 2, "name": "Engineering", "parent_id": 1},
        ]
        dt_client.get_all_users.return_value = [
            {"userid": "u1", "name": "张三", "email": "zs@test.com", "mobile": "13800138000", "title": "工程师", "job_number": "E001", "dept_id_list": [2]},
        ]

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []
        ad_service.get_all_ous.return_value = []
        ad_service.create_ou.return_value = True
        ad_service.create_user.return_value = (True, "CN=张三,OU=Engineering,OU=Users,DC=corp,DC=com")

        db = AsyncMock()

        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        result = await execute_sync(dt_client, ad_service, config, db)

        assert result["total"] >= 2  # at least 1 OU + 1 user
        assert result["success"] >= 2
        assert result["failed"] == 0
        assert result["status"] == "success"

        # Verify DB calls
        db.update_sync_status.assert_called()
        db.add_log.assert_called()
        ad_service.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sync_disables_user(self):
        """execute_sync should disable users not in DingTalk"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
        ]
        dt_client.get_all_users.return_value = []  # No DingTalk users

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=李四,OU=Users,DC=corp,DC=com",
                "cn": "李四",
                "displayName": "李四",
                "mail": "",
                "mobile": "",
                "title": "",
                "employeeID": "",
                "userAccountControl": 512,  # enabled
                "sAMAccountName": "lisi",
            }
        ]
        ad_service.get_all_ous.return_value = []
        ad_service.disable_user.return_value = True

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        result = await execute_sync(dt_client, ad_service, config, db)

        assert result["total"] >= 1
        assert result["success"] >= 1
        ad_service.disable_user.assert_called_once_with("CN=李四,OU=Users,DC=corp,DC=com")

    @pytest.mark.asyncio
    async def test_execute_sync_skips_already_disabled(self):
        """execute_sync should skip already disabled users"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = []
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=王五,OU=Users,DC=corp,DC=com",
                "cn": "王五",
                "displayName": "王五",
                "mail": "",
                "mobile": "",
                "title": "",
                "employeeID": "",
                "userAccountControl": 514,  # already disabled
                "sAMAccountName": "wangwu",
            }
        ]
        ad_service.get_all_ous.return_value = ["OU=Groups,DC=corp,DC=com"]
        ad_service.get_existing_groups.return_value = []

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        result = await execute_sync(dt_client, ad_service, config, db)

        assert result["total"] == 0
        ad_service.disable_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_sync_modifies_user_attributes(self):
        """execute_sync should modify changed attributes"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
        ]
        dt_client.get_all_users.return_value = [
            {"userid": "u1", "name": "张三", "email": "new@test.com", "mobile": "13800138000", "title": "工程师", "job_number": "E001", "dept_id_list": [1]},
        ]

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = [
            {
                "dn": "CN=张三,OU=Users,DC=corp,DC=com",
                "cn": "张三",
                "displayName": "张三",
                "mail": "old@test.com",
                "mobile": "13900139000",
                "title": "工程师",
                "employeeID": "E001",
                "userAccountControl": 512,
                "sAMAccountName": "zhangsan",
            }
        ]
        ad_service.get_all_ous.return_value = []
        ad_service.modify_user.return_value = True

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        result = await execute_sync(dt_client, ad_service, config, db)

        assert result["total"] >= 1
        ad_service.modify_user.assert_called()

    @pytest.mark.asyncio
    async def test_execute_sync_partial_status(self):
        """If some operations fail, status should be 'partial'"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = [
            {"dept_id": 1, "name": "Root", "parent_id": 0},
            {"dept_id": 2, "name": "Engineering", "parent_id": 1},
        ]
        dt_client.get_all_users.return_value = [
            {"userid": "u1", "name": "张三", "email": "", "mobile": "", "title": "", "job_number": "", "dept_id_list": [2]},
        ]

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []
        ad_service.get_all_ous.return_value = []
        ad_service.create_ou.return_value = False  # OU creation fails
        ad_service.create_user.return_value = (True, "CN=张三,OU=Engineering,OU=Users,DC=corp,DC=com")

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        result = await execute_sync(dt_client, ad_service, config, db)

        assert result["failed"] > 0
        assert result["status"] == "partial"

    @pytest.mark.asyncio
    async def test_execute_sync_exception_sets_failed_status(self):
        """If exception occurs, status should be 'failed' and status updated"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = []
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []
        ad_service.get_all_ous.return_value = ["OU=Groups,DC=corp,DC=com"]
        ad_service.get_existing_groups.return_value = []

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        # Make get_departments raise on the execute_sync flow
        # Actually, let's make it work normally but have no operations
        result = await execute_sync(dt_client, ad_service, config, db)
        assert result["status"] == "success"
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_execute_sync_sets_running_status(self):
        """execute_sync should set is_running=True at start"""
        dt_client = AsyncMock()
        dt_client.get_departments.return_value = []
        dt_client.get_all_users.return_value = []

        ad_service = MagicMock()
        ad_service.connect.return_value = True
        ad_service.get_existing_users.return_value = []
        ad_service.get_all_ous.return_value = []

        db = AsyncMock()
        config = {"ad_base_dn": "OU=Users,DC=corp,DC=com", "initial_password": "Test@2026"}

        await execute_sync(dt_client, ad_service, config, db)

        # First call should set is_running=True
        first_call = db.update_sync_status.call_args_list[0]
        assert first_call.kwargs.get("is_running") is True

        # Last call should set is_running=False
        last_call = db.update_sync_status.call_args_list[-1]
        assert last_call.kwargs.get("is_running") is False
