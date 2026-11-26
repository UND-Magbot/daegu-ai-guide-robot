FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 tzdata ffmpeg \
    && ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && echo "Asia/Seoul" > /etc/timezone \

    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 백엔드 포트 — docker-compose의 environment(PORT=...)로 덮어쓰기 가능.
# 기본 8010 
ENV PORT=8010
EXPOSE 8010

# sh -c + exec 패턴: ${PORT} 환경변수 치환을 받고, uvicorn이 PID 1로 승격되어
# SIGTERM/SIGINT(컨테이너 정지·재시작)를 정상 수신한다.
# keep-alive 30초: 기본 5초면 Jetson 무선 지연이 튈 때 3초 폴링이 서버가 막 닫은
# 연결을 재사용해 RemoteDisconnected 가 난다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 30"]
