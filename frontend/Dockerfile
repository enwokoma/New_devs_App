# The repository ships the compiled frontend in ./dist (no package.json or src/),
# so the previous multi-stage "npm install && npm run build" image could not
# build. Serve the prebuilt bundle directly with nginx.
FROM nginx:alpine
COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
