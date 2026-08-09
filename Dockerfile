FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 ecobee && mkdir -p /var/data && chown -R ecobee:ecobee /app /var/data
COPY --chown=ecobee:ecobee . .
USER ecobee
ENV ECOBEE_ENV=production
ENV ECOBEE_HOST=0.0.0.0
ENV ECOBEE_DB=/var/data/ecobee.db
VOLUME ["/var/data"]
EXPOSE 8000
CMD ["python3", "server.py"]
