# CiteMind 前端网关镜像：Node 构建前端产物，再把静态文件交给非 root nginx。
# 由 deploy/compose/compose.yml 的 frontend-gateway 服务以仓库根目录作为构建上下文构建；
# 根 .dockerignore 排除了 frontend，因此同目录的 frontend.Dockerfile.dockerignore 单独放行
# 根 workspace 清单、frontend 源与网关配置（BuildKit 按 <dockerfile>.dockerignore 选择）。
# 基础镜像按顶层 manifest digest 固定，不使用浮动 latest；build stage 的 node/pnpm
# 不进入运行镜像，运行镜像以非 root 的 nginx-unprivileged 提供 8080。

FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS build

# Node 自带的 corepack 按根 package.json 的 packageManager 字段准备 pnpm 11.22.0。
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable

WORKDIR /workspace

# 依赖层只依赖清单与锁文件，源码改动不会使依赖重新解析。
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json frontend/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --filter @citemind/frontend... --store-dir=/pnpm-store

COPY frontend ./frontend
RUN pnpm --filter @citemind/frontend build

FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:e75f89810bf5bfbcf58a1cfb32a1a11de55b7623d732e67735d513b720d7436a AS runtime

# 运行镜像默认已是非 root；配置在构建期解析校验，坏配置会让构建失败。
COPY deploy/compose/gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/frontend/dist /usr/share/nginx/html
RUN nginx -t

EXPOSE 8080
