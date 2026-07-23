"""
Test suite for multi-department security group feature
Tests: database primary_dept CRUD, ad_sync helper functions,
       preview_sync signature, main.py API routes, frontend consistency
"""
import asyncio
import os
import sys
import inspect
import ast
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database
from database import Database, init_db, DEFAULT_CONFIG
import ad_sync
from ad_sync import (
    get_groups_ou_path,
    get_security_group_name,
    preview_sync,
    MODIFY_ADD,
    MODIFY_DELETE,
)
import main as main_module


# ==================== 1. Syntax and Import Checks ====================

class TestSyntaxAndImports:
    """Verify syntax validity and correct imports"""

    def test_ad_sync_imports_modify_add(self):
        """MODIFY_ADD must be imported from ldap3"""
        assert MODIFY_ADD is not None
        from ldap3 import MODIFY_ADD as LDAP_MODIFY_ADD
        assert MODIFY_ADD == LDAP_MODIFY_ADD

    def test_ad_sync_imports_modify_delete(self):
        """MODIFY_DELETE must be imported from ldap3"""
        assert MODIFY_DELETE is not None
        from ldap3 import MODIFY_DELETE as LDAP_MODIFY_DELETE
        assert MODIFY_DELETE == LDAP_MODIFY_DELETE

    def test_database_imports_aiosqlite(self):
        """database.py must import aiosqlite"""
        assert hasattr(database, "aiosqlite")

    def test_database_imports_datetime(self):
        """database.py must import datetime"""
        assert hasattr(database, "datetime")

    def test_ad_groups_ou_in_default_config(self):
        """ad_groups_ou must exist in DEFAULT_CONFIG"""
        assert "ad_groups_ou" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["ad_groups_ou"] == ""

    def test_database_py_syntax_valid(self):
        """database.py must have no syntax errors"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        db_file = os.path.join(backend_dir, "database.py")
        with open(db_file, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)

    def test_ad_sync_py_syntax_valid(self):
        """ad_sync.py must have no syntax errors"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)

    def test_main_py_syntax_valid(self):
        """main.py must have no syntax errors"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        main_file = os.path.join(backend_dir, "main.py")
        with open(main_file, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source)

    def test_frontend_html_syntax_valid(self):
        """frontend/index.html must exist and be readable"""
        frontend_file = os.path.join(
            os.path.dirname(__file__), "..", "frontend", "index.html"
        )
        assert os.path.exists(frontend_file)
        with open(frontend_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert len(content) > 0


# ==================== 2. database.py New Methods Tests ====================

@pytest.fixture
async def test_db(tmp_path):
    """Create an isolated test database"""
    db_dir = str(tmp_path / "multidept_test_db")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "sync.db")

    with patch.object(database, "DB_DIR", db_dir), \
         patch.object(database, "DB_PATH", db_path):
        await init_db()
        yield db_path


class TestUserPrimaryDeptTable:
    """Test user_primary_dept table creation"""

    @pytest.mark.asyncio
    async def test_table_created_after_init(self, tmp_path):
        """user_primary_dept table should exist after init_db()"""
        db_dir = str(tmp_path / "table_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='user_primary_dept'"
                )
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == "user_primary_dept"


class TestPrimaryDeptCRUD:
    """Test CRUD operations on user_primary_dept"""

    @pytest.mark.asyncio
    async def test_set_and_get_primary_dept(self, test_db):
        """set_user_primary_dept then get should return the set value"""
        await Database.set_user_primary_dept("user001", 100)
        result = await Database.get_user_primary_dept("user001")
        assert result == 100

    @pytest.mark.asyncio
    async def test_update_primary_dept(self, test_db):
        """Setting again should UPDATE (not insert duplicate)"""
        await Database.set_user_primary_dept("user001", 100)
        await Database.set_user_primary_dept("user001", 200)
        result = await Database.get_user_primary_dept("user001")
        assert result == 200

    @pytest.mark.asyncio
    async def test_get_all_primary_depts(self, test_db):
        """get_all_primary_depts should return dict of all records"""
        await Database.set_user_primary_dept("user001", 100)
        await Database.set_user_primary_dept("user002", 200)
        await Database.set_user_primary_dept("user003", 300)

        result = await Database.get_all_primary_depts()
        assert isinstance(result, dict)
        assert result["user001"] == 100
        assert result["user002"] == 200
        assert result["user003"] == 300
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_delete_primary_dept(self, test_db):
        """delete_user_primary_dept should remove the record"""
        await Database.set_user_primary_dept("user001", 100)
        assert await Database.get_user_primary_dept("user001") == 100

        await Database.delete_user_primary_dept("user001")
        result = await Database.get_user_primary_dept("user001")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_primary_dept(self, test_db):
        """get_user_primary_dept for non-existent user should return None"""
        result = await Database.get_user_primary_dept("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_all_primary_depts_empty(self, test_db):
        """get_all_primary_depts on empty table should return empty dict"""
        result = await Database.get_all_primary_depts()
        assert result == {}


# ==================== 3. ad_sync.py Helper Function Tests ====================

class TestGetGroupsOuPath:
    """Test get_groups_ou_path function"""

    BASE_DN = "OU=Users,OU=REALMAN,DC=corp,DC=realman-robot,DC=com"

    def test_auto_derive_empty_config(self):
        """With empty config dict, should auto-derive OU=Groups"""
        result = get_groups_ou_path(self.BASE_DN, {})
        assert result == "OU=Groups,OU=REALMAN,DC=corp,DC=realman-robot,DC=com"

    def test_auto_derive_none_config(self):
        """With None config, should auto-derive OU=Groups"""
        result = get_groups_ou_path(self.BASE_DN, None)
        assert result == "OU=Groups,OU=REALMAN,DC=corp,DC=realman-robot,DC=com"

    def test_auto_derive_no_config_arg(self):
        """With no config arg (default), should auto-derive OU=Groups"""
        result = get_groups_ou_path(self.BASE_DN)
        assert result == "OU=Groups,OU=REALMAN,DC=corp,DC=realman-robot,DC=com"

    def test_custom_config(self):
        """With ad_groups_ou set in config, should use it"""
        config = {"ad_groups_ou": "OU=MyGroups,DC=example,DC=com"}
        result = get_groups_ou_path(self.BASE_DN, config)
        assert result == "OU=MyGroups,DC=example,DC=com"

    def test_whitespace_config_stripped(self):
        """Config with whitespace should be stripped"""
        config = {"ad_groups_ou": "  OU=Trimmed,DC=test,DC=com  "}
        result = get_groups_ou_path(self.BASE_DN, config)
        assert result == "OU=Trimmed,DC=test,DC=com"

    def test_empty_string_config(self):
        """Empty string in ad_groups_ou should trigger auto-derive"""
        config = {"ad_groups_ou": ""}
        result = get_groups_ou_path(self.BASE_DN, config)
        assert result == "OU=Groups,OU=REALMAN,DC=corp,DC=realman-robot,DC=com"


class TestGetSecurityGroupName:
    """Test get_security_group_name function"""

    def test_chinese_dept_name(self):
        """Chinese department name should get SG_ prefix"""
        assert get_security_group_name("研发部") == "SG_研发部"

    def test_chinese_mixed_name(self):
        """Mixed Chinese/alpha name should get SG_ prefix"""
        assert get_security_group_name("项目A组") == "SG_项目A组"

    def test_english_name(self):
        """English department name should get SG_ prefix"""
        assert get_security_group_name("Engineering") == "SG_Engineering"

    def test_empty_name(self):
        """Empty string should return SG_"""
        assert get_security_group_name("") == "SG_"


# ==================== 4. preview_sync Signature Compatibility ====================

class TestPreviewSyncSignature:
    """Test preview_sync function signature"""

    def test_has_db_parameter(self):
        """preview_sync must have db=None parameter"""
        sig = inspect.signature(preview_sync)
        assert "db" in sig.parameters
        db_param = sig.parameters["db"]
        assert db_param.default is None
        assert db_param.default == None

    def test_db_parameter_optional(self):
        """db parameter should have a default value (optional)"""
        sig = inspect.signature(preview_sync)
        db_param = sig.parameters["db"]
        assert db_param.default != inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_preview_sync_with_db_none_guard(self, tmp_path):
        """preview_sync with db=None should not crash on primary_depts_override access"""
        # Read source to verify the `if db:` guard exists
        import re as re_module
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        # Check that `if db:` guard exists before accessing primary_depts_override
        assert "if db:" in source
        # The db check should guard the get_all_primary_depts call
        assert "if db:" in source and "get_all_primary_depts" in source


# ==================== 5. main.py API Route Checks ====================

class TestMainPyRoutes:
    """Test main.py API routes and models"""

    def test_primary_dept_update_model(self):
        """PrimaryDeptUpdate model must have primary_dept_id: int field"""
        assert hasattr(main_module, "PrimaryDeptUpdate")
        model_fields = main_module.PrimaryDeptUpdate.model_fields
        assert "primary_dept_id" in model_fields
        # Verify the field is an integer type
        field_info = model_fields["primary_dept_id"]
        assert field_info.annotation == int

    def test_get_multi_dept_users_route_exists(self):
        """GET /api/multi-dept/users route must be registered"""
        routes = [r.path for r in main_module.app.routes if hasattr(r, "methods")]
        found = False
        for route in main_module.app.routes:
            if hasattr(route, "path") and route.path == "/api/multi-dept/users":
                if hasattr(route, "methods") and "GET" in route.methods:
                    found = True
                    break
        assert found, "GET /api/multi-dept/users route not found"

    def test_set_primary_dept_route_exists(self):
        """PUT /api/multi-dept/{userid}/primary route must be registered"""
        found = False
        for route in main_module.app.routes:
            if hasattr(route, "path") and route.path == "/api/multi-dept/{userid}/primary":
                if hasattr(route, "methods") and "PUT" in route.methods:
                    found = True
                    break
        assert found, "PUT /api/multi-dept/{userid}/primary route not found"

    def test_delete_primary_dept_route_exists(self):
        """DELETE /api/multi-dept/{userid}/primary route must be registered"""
        found = False
        for route in main_module.app.routes:
            if hasattr(route, "path") and route.path == "/api/multi-dept/{userid}/primary":
                if hasattr(route, "methods") and "DELETE" in route.methods:
                    found = True
                    break
        assert found, "DELETE /api/multi-dept/{userid}/primary route not found"

    def test_sync_preview_passes_database(self):
        """sync_preview route must pass Database as db param to preview_sync"""
        # Read main.py source to verify the call
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        main_file = os.path.join(backend_dir, "main.py")
        with open(main_file, "r", encoding="utf-8") as f:
            source = f.read()
        # Check that preview_sync is called with Database as the 4th argument
        assert "preview_sync(client, ad_service, config, Database)" in source

    def test_config_update_model_has_ad_groups_ou(self):
        """ConfigUpdate model must include ad_groups_ou field"""
        assert hasattr(main_module, "ConfigUpdate")
        model_fields = main_module.ConfigUpdate.model_fields
        assert "ad_groups_ou" in model_fields


# ==================== 6. Frontend Consistency Checks ====================

class TestFrontendConsistency:
    """Test frontend/index.html for multi-dept feature elements"""

    @pytest.fixture
    def frontend_html(self):
        frontend_file = os.path.join(
            os.path.dirname(__file__), "..", "frontend", "index.html"
        )
        with open(frontend_file, "r", encoding="utf-8") as f:
            return f.read()

    def test_sidebar_multidept_menu(self, frontend_html):
        """Sidebar must have index='multidept' menu item"""
        assert 'index="multidept"' in frontend_html

    def test_multidept_page_area(self, frontend_html):
        """Must have v-show='activeMenu === multidept' page area"""
        assert "activeMenu === 'multidept'" in frontend_html

    def test_handle_menu_select_multidept(self, frontend_html):
        """handleMenuSelect must handle 'multidept' branch"""
        assert "'multidept'" in frontend_html
        assert "loadMultiDeptUsers" in frontend_html

    def test_methods_exposed_in_return(self, frontend_html):
        """loadMultiDeptUsers, onPrimaryDeptChange, resetPrimaryDept must be in return"""
        # Find the return block
        return_section = frontend_html[frontend_html.index("return {"):]
        assert "loadMultiDeptUsers" in return_section
        assert "onPrimaryDeptChange" in return_section
        assert "resetPrimaryDept" in return_section

    def test_preview_data_has_new_groups(self, frontend_html):
        """previewData reactive object must include new_groups field"""
        # Find previewData definition
        pd_start = frontend_html.index("const previewData = reactive(")
        pd_end = frontend_html.index("});", pd_start) + 3
        pd_block = frontend_html[pd_start:pd_end]
        assert "new_groups" in pd_block

    def test_preview_data_has_group_membership(self, frontend_html):
        """previewData reactive object must include group_membership field"""
        pd_start = frontend_html.index("const previewData = reactive(")
        pd_end = frontend_html.index("});", pd_start) + 3
        pd_block = frontend_html[pd_start:pd_end]
        assert "group_membership" in pd_block

    def test_log_filter_has_create_group(self, frontend_html):
        """Log filter dropdown must have create_group option"""
        assert 'value="create_group"' in frontend_html

    def test_log_filter_has_add_to_group(self, frontend_html):
        """Log filter dropdown must have add_to_group option"""
        assert 'value="add_to_group"' in frontend_html

    def test_log_filter_has_remove_from_group(self, frontend_html):
        """Log filter dropdown must have remove_from_group option"""
        assert 'value="remove_from_group"' in frontend_html

    def test_get_op_text_handles_new_types(self, frontend_html):
        """getOpText must handle create_group, add_to_group, remove_from_group"""
        # Find getOpText function
        got_start = frontend_html.index("function getOpText")
        got_end = frontend_html.index("}", frontend_html.index("return map[type] || type;", got_start)) + 1
        got_block = frontend_html[got_start:got_end]
        assert "'create_group'" in got_block
        assert "'add_to_group'" in got_block
        assert "'remove_from_group'" in got_block

    def test_get_op_tag_type_handles_new_types(self, frontend_html):
        """getOpTagType must handle create_group, add_to_group, remove_from_group"""
        gott_start = frontend_html.index("function getOpTagType")
        gott_end = frontend_html.index("}", frontend_html.index("return map[type] || 'info'", gott_start)) + 1
        gott_block = frontend_html[gott_start:gott_end]
        assert "'create_group'" in gott_block
        assert "'add_to_group'" in gott_block
        assert "'remove_from_group'" in gott_block


# ==================== 7. ADSyncService Method Existence ====================

class TestADSyncServiceMethods:
    """Verify new ADSyncService methods exist with correct signatures"""

    def test_create_security_group_exists(self):
        """ADSyncService must have create_security_group method"""
        assert hasattr(ad_sync.ADSyncService, "create_security_group")

    def test_add_user_to_group_exists(self):
        """ADSyncService must have add_user_to_group method"""
        assert hasattr(ad_sync.ADSyncService, "add_user_to_group")

    def test_remove_user_from_group_exists(self):
        """ADSyncService must have remove_user_from_group method"""
        assert hasattr(ad_sync.ADSyncService, "remove_user_from_group")

    def test_get_user_group_memberships_exists(self):
        """ADSyncService must have get_user_group_memberships method"""
        assert hasattr(ad_sync.ADSyncService, "get_user_group_memberships")

    def test_get_existing_groups_exists(self):
        """ADSyncService must have get_existing_groups method"""
        assert hasattr(ad_sync.ADSyncService, "get_existing_groups")

    def test_create_security_group_params(self):
        """create_security_group should accept group_name and parent_dn"""
        sig = inspect.signature(ad_sync.ADSyncService.create_security_group)
        params = list(sig.parameters.keys())
        # 'self' is first
        assert "group_name" in params
        assert "parent_dn" in params

    def test_add_user_to_group_params(self):
        """add_user_to_group should accept user_dn and group_dn"""
        sig = inspect.signature(ad_sync.ADSyncService.add_user_to_group)
        params = list(sig.parameters.keys())
        assert "user_dn" in params
        assert "group_dn" in params

    def test_remove_user_from_group_params(self):
        """remove_user_from_group should accept user_dn and group_dn"""
        sig = inspect.signature(ad_sync.ADSyncService.remove_user_from_group)
        params = list(sig.parameters.keys())
        assert "user_dn" in params
        assert "group_dn" in params


# ==================== 8. execute_sync Security Group Logic ====================

class TestExecuteSyncLogic:
    """Verify execute_sync contains security group sync logic"""

    def test_execute_sync_has_groups_ou(self):
        """execute_sync must call get_groups_ou_path"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        # Find execute_sync function body
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert "get_groups_ou_path" in exec_body

    def test_execute_sync_creates_security_groups(self):
        """execute_sync must call create_security_group"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert "create_security_group" in exec_body

    def test_execute_sync_adds_users_to_groups(self):
        """execute_sync must call add_user_to_group"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert "add_user_to_group" in exec_body

    def test_execute_sync_removes_users_from_groups(self):
        """execute_sync must call remove_user_from_group"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert "remove_user_from_group" in exec_body

    def test_execute_sync_logs_create_group(self):
        """execute_sync must log with operation_type='create_group'"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert '"create_group"' in exec_body or "'create_group'" in exec_body

    def test_execute_sync_logs_add_to_group(self):
        """execute_sync must log with operation_type='add_to_group'"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert '"add_to_group"' in exec_body or "'add_to_group'" in exec_body

    def test_execute_sync_logs_remove_from_group(self):
        """execute_sync must log with operation_type='remove_from_group'"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        exec_start = source.index("async def execute_sync")
        exec_end = len(source)  # execute_sync is the last function in the file
        exec_body = source[exec_start:exec_end]
        assert '"remove_from_group"' in exec_body or "'remove_from_group'" in exec_body

    def test_preview_sync_has_new_groups_in_return(self):
        """preview_sync return dict must include new_groups"""
        backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
        ad_file = os.path.join(backend_dir, "ad_sync.py")
        with open(ad_file, "r", encoding="utf-8") as f:
            source = f.read()
        # Find preview_sync return statement
        preview_start = source.index("async def preview_sync")
        # Find the return dict
        return_start = source.index("return {", preview_start)
        return_end = source.index("}", source.index("dingtalk_dept_count", return_start)) + 1
        return_block = source[return_start:return_end]
        assert "new_groups" in return_block
        assert "group_membership" in return_block
