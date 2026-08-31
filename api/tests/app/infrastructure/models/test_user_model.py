#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_user_model.py
UserModel 单元测试 — 验证 F4-代码质量修复: TYPE_CHECKING 延迟导入与往返转换一致性

测试覆盖:
- from_domain: User(领域模型) -> UserModel(ORM) 字段完整映射
- to_domain:   UserModel(ORM) -> User(领域模型) 字段完整映射
- 往返一致性: User -> UserModel -> User 字段无丢失/无类型漂移
- role 字段: 枚举 <-> 字符串双向转换
- TYPE_CHECKING 守卫: 运行时未触发未使用导入告警
"""
from datetime import datetime

from app.domain.models.user import User, UserRole
from app.infrastructure.models.user import UserModel


# ============ 辅助构造函数 ============

def _make_domain_user(
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
) -> User:
    """构造测试用领域 User"""
    return User(
        id="user-uuid-001",
        username="admin",
        phone="13800000000",
        hashed_password="$2b$12$hashedsecret",
        role=role,
        is_active=is_active,
        updated_at=datetime(2026, 7, 20, 10, 0, 0),
        created_at=datetime(2026, 7, 1, 9, 0, 0),
    )


def _make_orm_user(
    role: str = "admin",
    is_active: bool = True,
) -> UserModel:
    """构造测试用 ORM UserModel"""
    return UserModel(
        id="user-uuid-001",
        username="admin",
        phone="13800000000",
        hashed_password="$2b$12$hashedsecret",
        role=role,
        is_active=is_active,
        updated_at=datetime(2026, 7, 20, 10, 0, 0),
        created_at=datetime(2026, 7, 1, 9, 0, 0),
    )


# ============ from_domain: User -> UserModel ============

class TestFromDomain:
    """UserModel.from_domain 转换验证"""

    def test_from_domain_maps_all_fields(self):
        """from_domain 应完整映射所有字段"""
        user = _make_domain_user()
        model = UserModel.from_domain(user)

        assert model.id == user.id
        assert model.username == user.username
        assert model.phone == user.phone
        assert model.hashed_password == user.hashed_password
        assert model.is_active == user.is_active
        assert model.updated_at == user.updated_at
        assert model.created_at == user.created_at

    def test_from_domain_converts_role_enum_to_string(self):
        """from_domain 应将 UserRole 枚举转换为字符串值"""
        user = _make_domain_user(role=UserRole.ADMIN)
        model = UserModel.from_domain(user)
        assert model.role == "admin"

    def test_from_domain_converts_user_role_enum(self):
        """from_domain 应正确处理 USER 角色枚举"""
        user = _make_domain_user(role=UserRole.USER)
        model = UserModel.from_domain(user)
        assert model.role == "user"

    def test_from_domain_accepts_string_role(self):
        """from_domain 应兼容直接传入字符串 role(降级路径)"""
        user = _make_domain_user()
        user.role = "admin"  # 强制设为字符串,绕过枚举
        model = UserModel.from_domain(user)
        assert model.role == "admin"


# ============ to_domain: UserModel -> User ============

class TestToDomain:
    """UserModel.to_domain 转换验证"""

    def test_to_domain_maps_all_fields(self):
        """to_domain 应完整映射所有字段"""
        model = _make_orm_user()
        user = model.to_domain()

        assert user.id == model.id
        assert user.username == model.username
        assert user.phone == model.phone
        assert user.hashed_password == model.hashed_password
        assert user.is_active == model.is_active
        assert user.updated_at == model.updated_at
        assert user.created_at == model.created_at

    def test_to_domain_converts_role_string_to_enum(self):
        """to_domain 应将字符串 role 转换为 UserRole 枚举"""
        model = _make_orm_user(role="admin")
        user = model.to_domain()
        assert user.role == UserRole.ADMIN

    def test_to_domain_converts_user_role_string(self):
        """to_domain 应正确处理 user 角色字符串"""
        model = _make_orm_user(role="user")
        user = model.to_domain()
        assert user.role == UserRole.USER


# ============ 往返一致性 ============

class TestRoundTrip:
    """User <-> UserModel 往返转换一致性"""

    def test_roundtrip_preserves_all_fields_admin(self):
        """User -> UserModel -> User 往返应保留所有字段(ADMIN)"""
        original = _make_domain_user(role=UserRole.ADMIN)
        restored = UserModel.from_domain(original).to_domain()

        assert restored.id == original.id
        assert restored.username == original.username
        assert restored.phone == original.phone
        assert restored.hashed_password == original.hashed_password
        assert restored.role == original.role
        assert restored.is_active == original.is_active
        assert restored.updated_at == original.updated_at
        assert restored.created_at == original.created_at

    def test_roundtrip_preserves_all_fields_user(self):
        """User -> UserModel -> User 往返应保留所有字段(USER)"""
        original = _make_domain_user(role=UserRole.USER, is_active=False)
        restored = UserModel.from_domain(original).to_domain()

        assert restored.role == UserRole.USER
        assert restored.is_active is False

    def test_roundtrip_role_enum_type_restored(self):
        """往返后 role 应恢复为 UserRole 枚举类型(非字符串)"""
        original = _make_domain_user(role=UserRole.ADMIN)
        restored = UserModel.from_domain(original).to_domain()

        assert isinstance(restored.role, UserRole)
        assert restored.role == UserRole.ADMIN


# ============ TYPE_CHECKING 守卫验证 ============

class TestTypeCheckingGuard:
    """验证 TYPE_CHECKING 守卫不会在运行时引入未使用导入"""

    def test_module_imports_without_runtime_user_import(self):
        """模块应可在运行时导入,User 仅在 TYPE_CHECKING 块内可见"""
        import app.infrastructure.models.user as user_module

        # TYPE_CHECKING 块在运行时不执行,所以 User 不应是模块级属性
        # 这避免了循环依赖与未使用导入告警
        assert not hasattr(user_module, "User")

    def test_type_checking_block_present_in_source(self):
        """模块源码应包含 TYPE_CHECKING 守卫块"""
        import app.infrastructure.models.user as user_module

        source = open(user_module.__file__, encoding="utf-8").read()
        assert "from typing import TYPE_CHECKING" in source
        assert "if TYPE_CHECKING:" in source

    def test_future_annotations_import_present(self):
        """模块应启用 from __future__ import annotations (PEP 563)"""
        import app.infrastructure.models.user as user_module

        source = open(user_module.__file__, encoding="utf-8").read()
        assert "from __future__ import annotations" in source

