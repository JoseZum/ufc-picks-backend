FROM nginx:alpine

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/ /etc/nginx/conf.d/

# Expose ports
EXPOSE 80 443

# Run nginx
CMD ["nginx", "-g", "daemon off;"]
