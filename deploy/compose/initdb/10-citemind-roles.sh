#!/bin/sh
# CiteMind 本地数据服务初始化：官方 postgres 镜像只在空数据卷上执行本脚本。
#
# 创建 citemind_api 与 citemind_worker 两个非特权 LOGIN 角色，创建 citemind_test 测试库，
# 并收紧 citemind 与 citemind_test 的 ACL：收回 PUBLIC 的数据库权限与 public schema 权限，
# 只给两个运行角色 CONNECT 与 schema USAGE；不授 CREATE、不授表权限，也不设置 ALTER DEFAULT PRIVILEGES。
#
# 这里不建扩展、不建业务表：vector 扩展由 Alembic 迁移创建，业务表仍未实现。
#
# 官方入口可能 exec（脚本有执行位）或 source（无执行位）本文件，因此不使用 set -e，
# 每条关键命令都显式检查退出状态；失败会中止容器初始化，需要 `down -v` 重建数据卷。

citemind_init_fail() {
	printf 'CiteMind initdb 失败：%s\n' "$1" >&2
	exit 1
}

citemind_init_roles() {
	if [ -z "${POSTGRES_USER:-}" ] || [ -z "${POSTGRES_DB:-}" ]; then
		citemind_init_fail "缺少 POSTGRES_USER 或 POSTGRES_DB"
	fi
	if [ -z "${CITEMIND_API_DB_PASSWORD:-}" ]; then
		citemind_init_fail "缺少 CITEMIND_API_DB_PASSWORD"
	fi
	if [ -z "${CITEMIND_WORKER_DB_PASSWORD:-}" ]; then
		citemind_init_fail "缺少 CITEMIND_WORKER_DB_PASSWORD"
	fi

	# 密码通过 psql 变量传入，由 psql 负责 SQL 字面量转义，不拼接字符串。
	psql --set=ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
		--set=citemind_api_password="$CITEMIND_API_DB_PASSWORD" \
		--set=citemind_worker_password="$CITEMIND_WORKER_DB_PASSWORD" <<'SQL' || citemind_init_fail "创建角色或测试库失败"
CREATE ROLE citemind_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	PASSWORD :'citemind_api_password';
CREATE ROLE citemind_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	PASSWORD :'citemind_worker_password';
CREATE DATABASE citemind_test;
SQL

	# 数据库级权限在集群范围内生效，在任一库里执行即可。
	psql --set=ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
		--set=citemind_app_db="$POSTGRES_DB" <<'SQL' || citemind_init_fail "收紧数据库级权限失败"
REVOKE ALL ON DATABASE :"citemind_app_db" FROM PUBLIC;
REVOKE ALL ON DATABASE citemind_test FROM PUBLIC;
GRANT CONNECT ON DATABASE :"citemind_app_db" TO citemind_api, citemind_worker;
GRANT CONNECT ON DATABASE citemind_test TO citemind_api, citemind_worker;
SQL

	# public schema 的 ACL 属于各个数据库，需要分别连接修改。
	for citemind_db in "$POSTGRES_DB" citemind_test; do
		psql --set=ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$citemind_db" <<'SQL' || citemind_init_fail "收紧 $citemind_db 的 public schema 权限失败"
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO citemind_api, citemind_worker;
SQL
	done

	# healthcheck 依赖该标记；只有上述所有步骤成功后才写入，防止半初始化卷被判为健康。
	: > "$PGDATA/.citemind-init-complete" || citemind_init_fail "无法写入初始化完成标记"
	printf 'CiteMind initdb 完成：已创建 citemind_api、citemind_worker、citemind_test 并收紧 PUBLIC 权限\n'
}

citemind_init_roles
