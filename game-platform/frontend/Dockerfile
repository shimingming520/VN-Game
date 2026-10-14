# 构建阶段:装依赖 + vite build 出静态文件
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# 运行阶段:nginx 只托管静态文件 + 反代 /api 到后端 Service
FROM nginx:1.27-alpine
ENV TZ=Asia/Shanghai
RUN apk add --no-cache curl
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
