# TeamHarness 前端镜像（规则库管理界面）
#
# 纯静态文件（HTML + JS + CDN 引入），nginx 托管。
# 无需 Node.js 构建，直接复制文件到 nginx html 目录。

FROM nginx:1.27-alpine

# 复制前端静态文件
COPY frontend/index.html /usr/share/nginx/html/index.html
COPY frontend/app.js /usr/share/nginx/html/app.js
COPY frontend/services/ /usr/share/nginx/html/services/

# nginx 配置由 docker-compose 挂载（含前端路由 + API 反代）
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
