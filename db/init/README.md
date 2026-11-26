# DB 초기 데이터 (최초 1회 자동 import)

`docker-compose.windows.yml` 의 db 컨테이너는 **DB 볼륨이 비어 있을 때만**
이 폴더의 `*.sql` / `*.sql.gz` 를 파일명 순서대로 실행한다.

기존 관제 PC 데이터를 옮길 때 덤프를 여기에 넣는다.

    docker exec control-db sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction --routines --triggers --databases control_system_dev' > db/init/01_control_system_dev.sql

- `*.sql` 은 .gitignore 대상이라 git 에 올라가지 않는다 (USB 등으로 별도 전달).
- 이미 볼륨이 있으면 무시된다. 다시 넣으려면 `docker compose down -v` 후 재기동(데이터 삭제 주의).
