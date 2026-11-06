-- LFLT Aging 요약 (Plan ID 7774 에 남아있는 Open 로드): 공유용 표 2개를 한 결과로
--   PG30,50,80 : Hold / Requested / Total x Drop in TMS 경과일 (0d, 1d, 2d, 3d, 4d+) 볼륨
--   PG20       : Hold / Requested / Total x RDD Passed / RDD Not Passed 볼륨
-- 대상: LD_LEG_T 상태 300(Open), LGC2·LGC7·LGC8 출발, Plan ID 7774 Shipment. 빈 로드(볼륨 0) 제외
-- Hold / Requested : 로드 External URL2 에 REQ → Requested, 그 외 → Hold
-- Drop in TMS 경과일 = 오늘 - Shipment TMS 생성일 (SHPM_T.CRTD_DTT, 달력일)
-- RDD Passed = 오늘이 RDD_TO 보다 뒤 (RDD 당일은 Not Passed)
-- 볼륨 = SHPM_T.VOL (0/없으면 IF VOLUME x PIECES). Hold / Requested 줄은 항상 나오고, 0 인 칸은 '-'
-- 실행: DBeaver에서 전체 선택(Ctrl + A) 후 Ctrl + Enter
WITH ld AS (
    SELECT /*+ MATERIALIZE INDEX(a LD_LEG_I11) */ a.LD_LEG_ID, a.EXTL_URL2
    FROM   TMS_PROD.LD_LEG_T a
    WHERE  a.CUR_OPTLSTAT_ID = 300
      AND  a.FRST_SHPG_LOC_CD IN ('LGC2', 'LGC7', 'LGC8')
),
x AS (
    SELECT /*+ MATERIALIZE USE_NL(t) INDEX(t LD_LEG_DETL_I11) */
           ld.LD_LEG_ID,
           CASE WHEN REGEXP_LIKE(UPPER(ld.EXTL_URL2), '(^|[^A-Z])REQ([^A-Z]|$)')
                THEN 'Requested' ELSE 'Hold' END                              AS STATUS,
           NVL(TO_CHAR(t.PLAN_ID), TO_CHAR(c.SYSTEMPLANID))                   AS PLAN_ID,
           REGEXP_SUBSTR(TO_CHAR(c.PLAN_ID_GRP), '\d+')                       AS PG,
           NVL(NULLIF(s.VOL, 0), c.VOLUME * NVL(c.PIECES, 1))                 AS VOL,
           TRUNC(SYSDATE) - TRUNC(s.CRTD_DTT)                                 AS AGE_DAYS,
           TRUNC(c.DELIVERYTODATETIME)                                        AS RDD_TO
    FROM   ld
    JOIN   TMS_PROD.LD_LEG_DETL_T t     ON t.LD_LEG_ID = ld.LD_LEG_ID
    LEFT   JOIN TMS_PROD.SHPM_T s       ON s.SHPM_NUM = t.SHPM_NUM
    LEFT   JOIN TMS_IF.IF_IN_SHIPMENT c ON c.SHIPMENTNUMBER = t.SHPM_NUM
                                       AND NVL(c.CANCEL_YN, 'N') = 'N'
),
f AS (
    SELECT x.*,
           CASE WHEN x.PG IN ('30', '50', '80') THEN 'PG30,50,80'
                WHEN x.PG = '20'                THEN 'PG20' END               AS PLAN_GROUP,
           SUM(NVL(x.VOL, 0)) OVER (PARTITION BY x.LD_LEG_ID)                 AS LOAD_VOL
    FROM   x
    WHERE  TRIM(x.PLAN_ID) = '7774'
),
k AS (                                                                         -- 행 틀: 0 이어도 다 나오게
    SELECT g.PLAN_GROUP, g.G_ORD, st.STATUS, st.S_ORD
    FROM  (SELECT 'PG30,50,80' AS PLAN_GROUP, 1 AS G_ORD FROM dual
           UNION ALL SELECT 'PG20', 2 FROM dual) g
    CROSS JOIN
          (SELECT 'Hold' AS STATUS, 1 AS S_ORD FROM dual
           UNION ALL SELECT 'Requested', 2 FROM dual) st
),
agg AS (
    SELECT f.PLAN_GROUP, f.STATUS,
           SUM(NVL(f.VOL, 0))                                                          AS VOLUME,
           SUM(CASE WHEN f.AGE_DAYS <= 0 THEN NVL(f.VOL, 0) ELSE 0 END)                AS D0,
           SUM(CASE WHEN f.AGE_DAYS = 1  THEN NVL(f.VOL, 0) ELSE 0 END)                AS D1,
           SUM(CASE WHEN f.AGE_DAYS = 2  THEN NVL(f.VOL, 0) ELSE 0 END)                AS D2,
           SUM(CASE WHEN f.AGE_DAYS = 3  THEN NVL(f.VOL, 0) ELSE 0 END)                AS D3,
           SUM(CASE WHEN f.AGE_DAYS >= 4 OR f.AGE_DAYS IS NULL THEN NVL(f.VOL, 0) ELSE 0 END) AS D4P,
           SUM(CASE WHEN TRUNC(SYSDATE) > f.RDD_TO THEN NVL(f.VOL, 0) ELSE 0 END)      AS RDD_PASSED,
           SUM(CASE WHEN f.RDD_TO IS NULL OR TRUNC(SYSDATE) <= f.RDD_TO
                    THEN NVL(f.VOL, 0) ELSE 0 END)                                     AS RDD_NOT_PASSED
    FROM   f
    WHERE  f.LOAD_VOL > 0                                                      -- 빈 로드 제외
      AND  f.PLAN_GROUP IS NOT NULL
    GROUP  BY f.PLAN_GROUP, f.STATUS
),
r AS (
    SELECT k.G_ORD, k.S_ORD, k.PLAN_GROUP, k.STATUS,
           NVL(agg.VOLUME, 0) AS VOLUME, NVL(agg.D0, 0) AS D0, NVL(agg.D1, 0) AS D1,
           NVL(agg.D2, 0) AS D2, NVL(agg.D3, 0) AS D3, NVL(agg.D4P, 0) AS D4P,
           NVL(agg.RDD_PASSED, 0) AS RDD_PASSED, NVL(agg.RDD_NOT_PASSED, 0) AS RDD_NOT_PASSED
    FROM   k
    LEFT   JOIN agg ON agg.PLAN_GROUP = k.PLAN_GROUP AND agg.STATUS = k.STATUS
    UNION ALL
    SELECT k2.G_ORD, 3, k2.PLAN_GROUP, 'Total',
           NVL(SUM(agg.VOLUME), 0), NVL(SUM(agg.D0), 0), NVL(SUM(agg.D1), 0), NVL(SUM(agg.D2), 0),
           NVL(SUM(agg.D3), 0), NVL(SUM(agg.D4P), 0), NVL(SUM(agg.RDD_PASSED), 0),
           NVL(SUM(agg.RDD_NOT_PASSED), 0)
    FROM  (SELECT DISTINCT PLAN_GROUP, G_ORD FROM k) k2
    LEFT   JOIN agg ON agg.PLAN_GROUP = k2.PLAN_GROUP
    GROUP  BY k2.G_ORD, k2.PLAN_GROUP
)
SELECT TRUNC(SYSDATE)                                                  AS AS_OF,
       CASE WHEN r.S_ORD = 1 THEN r.PLAN_GROUP END                     AS "Plan Group",
       r.STATUS                                                        AS "Status",
       CASE WHEN ROUND(r.VOLUME) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.VOLUME), 'FM999,999,990') END AS "Volume",
       CASE WHEN r.G_ORD = 1 THEN CASE WHEN ROUND(r.D0) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.D0), 'FM999,999,990') END END AS "0d (Vol)",
       CASE WHEN r.G_ORD = 1 THEN CASE WHEN ROUND(r.D1) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.D1), 'FM999,999,990') END END AS "1d (Vol)",
       CASE WHEN r.G_ORD = 1 THEN CASE WHEN ROUND(r.D2) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.D2), 'FM999,999,990') END END AS "2d (Vol)",
       CASE WHEN r.G_ORD = 1 THEN CASE WHEN ROUND(r.D3) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.D3), 'FM999,999,990') END END AS "3d (Vol)",
       CASE WHEN r.G_ORD = 1 THEN CASE WHEN ROUND(r.D4P) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.D4P), 'FM999,999,990') END END AS "4d+ (Vol)",
       CASE WHEN r.G_ORD = 2 THEN CASE WHEN ROUND(r.RDD_PASSED) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.RDD_PASSED), 'FM999,999,990') END END AS "RDD Passed (Vol)",
       CASE WHEN r.G_ORD = 2 THEN CASE WHEN ROUND(r.RDD_NOT_PASSED) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.RDD_NOT_PASSED), 'FM999,999,990') END END AS "RDD Not Passed (Vol)",
       CASE WHEN ROUND(r.VOLUME) = 0 THEN '-' ELSE TO_CHAR(ROUND(r.VOLUME), 'FM999,999,990') END AS "Total Vol"
FROM   r
ORDER  BY r.G_ORD, r.S_ORD
