# 基础镜像默认从自建 harbor 拉(见 ../creation-engine/mirror_base_images.sh)。
# 公共 Docker Hub 代理源现在基本都要认证或限流(daocloud/1ms/阿里云 401、xuanyuan 429),
# 官方源在国内又极慢 —— 本机是 arm64 而集群要 amd64,跨架构构建一个层都命中不了缓存,
# 每次都得重拉 50MB 基础镜像。harbor 在内网,不受公共源关停影响。
# 想临时走官方源:docker build --build-arg BASE_REGISTRY=docker.io/library ...
ARG BASE_REGISTRY=harbor.farmmx.com/agent-platform

# 构建阶段:装依赖 + vite build 出静态文件
#
# 基础镜像必须是 node:22+:vue-router 5 依赖的 @babel/generator 8 要求 node ^22.18 || >=24.11,
# 停在 node:20 会让 npm ci 刷满 EBADENGINE 警告。rolldown 的 wasm fallback 还带有
# @emnapi/* 可选 peer；package.json 显式声明这组 optionalDependencies，确保 npm 10/11
# 生成和校验同一份 lockfile 时都不会误判缺包。
FROM ${BASE_REGISTRY}/node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
# 走国内 registry:与仓库内其它 Dockerfile 的 pypi 镜像源一致,默认源在国内拉包极慢
RUN npm config set registry https://registry.npmmirror.com \
    && npm install --global npm@11.6.2 \
    && npm ci
COPY . .
RUN npm run build

# 运行阶段:nginx 只托管静态文件 + 反代 /api 到后端 Service
# ARG 在 FROM 之后会失效,多阶段里每个阶段前都要重新声明
ARG BASE_REGISTRY=harbor.farmmx.com/agent-platform
FROM ${BASE_REGISTRY}/nginx:1.27-alpine
ENV TZ=Asia/Shanghai
RUN sed -i 's#dl-cdn.alpinelinux.org#mirrors.aliyun.com#g' /etc/apk/repositories \
    && apk add --no-cache curl
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
