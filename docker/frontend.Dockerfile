FROM node:18-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json /app/
RUN npm ci

COPY frontend /app
RUN npm run build

FROM nginx:stable-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

