FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .

# Next.js inlines NEXT_PUBLIC_* variables into the JS bundle at build time,
# not read at container start -- a runtime-only env var would leave the
# deployed app calling localhost:8000 for every visitor. These must be
# passed in as --build-arg (Render passes configured service env vars as
# Docker build args automatically for docker-runtime services; verify this
# still holds for your account/plan if the deployed frontend ends up
# pointed at the wrong API URL).
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_ENVIRONMENT=production
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_ENVIRONMENT=$NEXT_PUBLIC_ENVIRONMENT

RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]
