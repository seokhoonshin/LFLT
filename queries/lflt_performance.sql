-- LFLT 실적 (11/2/2026 부터): LGC2·LGC7·LGC8 출발 서비스 LFLT 로드, 로드 1줄 = 1행 (로드 ID 중복 없음)
-- SHIPMENT_IDS = 그 로드의 쉽먼트 번호 전부 (하차 순서대로, 쉼표로 한 칸에)
-- 기준일 = 로드 시작(픽업) 예정일 STRD_DTT. 상태 320(Accepted) 이상만 = 실제로 나간/나갈 로드
--   (Open·Planned·Tendered·Rejected 300~315 도 보려면 아래 a.CUR_OPTLSTAT_ID >= 320 을 300 으로)
-- 볼륨·무게·Pieces·Declared/Order value = 로드에 실린 Shipment 합계 (SHPM_T, 없으면 IF)
-- STOPS = 도착 주소 수 (번지 + 도로명 첫 단어 + ZIP5). Stop Location ID 는 여러 주소가 같이 쓰는 경우가
--         있어서(DMG-DR-S 등) 쓰지 않음. 같은 건물에 고객명이 둘이어도 1 스탑
-- 실행: DBeaver에서 전체 선택(Ctrl + A) 후 Ctrl + Enter (이 파일엔 쿼리 하나만 있음)
WITH a AS (
    SELECT /*+ MATERIALIZE */ a.LD_LEG_ID, a.FRST_SHPG_LOC_CD, a.SRVC_CD, a.CUR_OPTLSTAT_ID,
           a.STRD_DTT, a.SHPD_DTT, a.FIXD_ITNR_DIST, a.NUM_STOP, a.LAST_PSTL_CD, a.EXTL_URL2
    FROM   TMS_PROD.LD_LEG_T a
    WHERE  a.FRST_SHPG_LOC_CD IN ('LGC2', 'LGC7', 'LGC8')
      AND  a.SRVC_CD = 'LFLT'
      AND  a.STRD_DTT >= TO_DATE(:start_date, 'YYYY-MM-DD')                                     -- ← 시작일
      AND  a.CUR_OPTLSTAT_ID >= 320                                            -- Accepted 이상
),
d AS (
    SELECT /*+ MATERIALIZE USE_NL(t) INDEX(t LD_LEG_DETL_I11) */ t.LD_LEG_ID, t.SHPM_NUM, t.CARR_CD,
           t.CUST_NAME, t.TO_SHPG_LOC_CD, t.TO_CTY_NAME, t.TO_PSTL_CD, t.DLVY_STOP_SEQ_NUM
    FROM   a
    JOIN   TMS_PROD.LD_LEG_DETL_T t ON t.LD_LEG_ID = a.LD_LEG_ID
),
s AS (
    SELECT /*+ MATERIALIZE */ d.LD_LEG_ID,
           COUNT(*)                                                           AS SHIPMENTS,
           COUNT(DISTINCT NVL(REGEXP_SUBSTR(UPPER(TRIM(c.STREET)), '^[0-9]+[A-Z]?\s+[A-Z0-9]+'),
                              d.TO_SHPG_LOC_CD)
                          || '|' || SUBSTR(NVL(c.POSTALCODE, d.TO_PSTL_CD), 1, 5))   AS STOPS,  -- 주소 기준
           COUNT(DISTINCT d.TO_SHPG_LOC_CD)                                   AS STOP_LOCATION_IDS, -- 참고
           MAX(d.CARR_CD)                                                     AS CARRIER,
           LISTAGG(d.SHPM_NUM, ', ') WITHIN GROUP (ORDER BY d.DLVY_STOP_SEQ_NUM, d.SHPM_NUM) AS SHIPMENT_IDS,
           MAX(d.CUST_NAME) KEEP (DENSE_RANK LAST ORDER BY d.DLVY_STOP_SEQ_NUM NULLS FIRST) AS LAST_CUSTOMER,
           MAX(d.TO_CTY_NAME) KEEP (DENSE_RANK LAST ORDER BY d.DLVY_STOP_SEQ_NUM NULLS FIRST) AS LAST_DROP,
           SUM(NVL(NULLIF(sh.VOL, 0), c.VOLUME * NVL(c.PIECES, 1)))           AS VOLUME,
           SUM(NVL(sh.NMNL_WGT, c.WGT * NVL(c.PIECES, 1)))                    AS WEIGHT,
           SUM(NVL(sh.TOT_PCE, c.PIECES))                                     AS PIECES,
           SUM(NVL(sh.DCLD_VAL_DLR, 0))                                       AS DECLARED_VALUE,
           SUM(NVL(sh.ODR_VAL_DLR, c.ORDERVALUE))                             AS ORDER_VALUE
    FROM   d
    LEFT   JOIN TMS_PROD.SHPM_T sh      ON sh.SHPM_NUM = d.SHPM_NUM
    LEFT   JOIN TMS_IF.IF_IN_SHIPMENT c ON c.SHIPMENTNUMBER = d.SHPM_NUM
                                       AND NVL(c.CANCEL_YN, 'N') = 'N'
    GROUP  BY d.LD_LEG_ID
)
SELECT a.LD_LEG_ID                                                            AS LOAD_ID,
       s.SHIPMENTS,
       s.SHIPMENT_IDS,
       TRUNC(a.STRD_DTT)                                                      AS PICKUP_DATE,
       TO_CHAR(a.STRD_DTT, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH')                 AS PICKUP_DAY,
       TO_CHAR(a.STRD_DTT, 'HH24:MI')                                         AS PICKUP_TIME,
       TRUNC(a.SHPD_DTT)                                                      AS SHIPPED_DATE,
       a.FRST_SHPG_LOC_CD                                                     AS ORIGIN,
       a.CUR_OPTLSTAT_ID                                                      AS STATUS,
       s.CARRIER,
       a.SRVC_CD                                                              AS SERVICE_CODE,
       s.STOPS,
       s.STOP_LOCATION_IDS,
       s.LAST_CUSTOMER,
       s.LAST_DROP,
       a.LAST_PSTL_CD                                                         AS LAST_ZIP,
       ROUND(a.FIXD_ITNR_DIST, 1)                                             AS DISTANCE,
       ROUND(s.VOLUME, 1)                                                     AS VOLUME,
       ROUND(100 * s.VOLUME / 800, 1)                                         AS VOLUME_UTIL_PCT,  -- 800 cu ft 기준
       ROUND(s.WEIGHT, 1)                                                     AS WEIGHT,
       s.PIECES,
       ROUND(s.DECLARED_VALUE, 2)                                             AS DECLARED_VALUE,
       ROUND(s.ORDER_VALUE, 2)                                                AS ORDER_VALUE,
       a.EXTL_URL2
FROM   a
JOIN   s ON s.LD_LEG_ID = a.LD_LEG_ID
ORDER  BY PICKUP_DATE, ORIGIN, LOAD_ID
