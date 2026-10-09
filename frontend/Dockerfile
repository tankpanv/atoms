FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_PREVIEW_ORIGIN=
ARG VITE_PREVIEW_PORT=28082
ENV VITE_PREVIEW_ORIGIN=$VITE_PREVIEW_ORIGIN VITE_PREVIEW_PORT=$VITE_PREVIEW_PORT
RUN npm run build

FROM nginx:stable-alpine
ARG VITE_PREVIEW_ORIGIN=
ARG VITE_PREVIEW_PORT=28082
LABEL atoms.preview-origin=$VITE_PREVIEW_ORIGIN atoms.preview-port=$VITE_PREVIEW_PORT
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 5173
