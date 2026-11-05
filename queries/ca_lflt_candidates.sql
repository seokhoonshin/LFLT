-- CA LFLT 테스트용 후보 추출: 모레(주말이면 월요일) 배송 기준, RouteOpt 업로드 형식
-- 미배정(LD_LEG_DETL_T 400번대) + Open 로드(LD_LEG_T 300, 서비스 미정 포함). 볼륨 = VOLUME x PIECES
-- OBS·BEST BUY 제외 / 주소 표준화(같은 위치 = 같은 스톱) / HA + Memo PALLETIZE = 팔레트
-- 볼륨 한도는 스톱 단위: 같은 출발 DC → 같은 도착 주소인 후보 Shipment 합계 (RouteOpt의 스톱과 동일 기준)
-- MJ-WEST PoC 기준: PoC에 있는 Ship-to는 LFLT? = Y 만 대상(N·미기재·Compliance 제외), PoC에 없는 Ship-to는 다른 조건대로
-- Plan type COMPLIANCE·DEDICATED_INTERNAL 제외. RDD는 판단에 쓰지 않음 (표시만)
-- Appointment 요청한 로드(LD_LEG_T.EXTL_URL2 에 REQ) 는 전부 제외 → 남은 건만 다시 최적화
-- Plan ID 7774 는 고객·PoC·Plan type·서비스·Plan group·거리·볼륨 조건과 관계없이 전부 대상
--   (TMS 미등록·REQ 요청 완료·주소 없음·CA/NV/AZ 밖 은 그대로 제외)
-- declared_value = TMS_PROD.SHPM_T.DCLD_VAL_DLR (Shipment 단위. 로드에 실린 Shipment 합계 = LD_LEG_T.DCLD_VAL_DLR)
-- [기간 제한 없음 · 속도 개선 버전] 현재 미배정·Open 건만 인덱스로 먼저 찾고, 중간 결과를 한 번만 계산(MATERIALIZE)
-- 실행: DBeaver에서 전체 선택(Ctrl + A) 후 Ctrl + Enter
WITH p AS (
    SELECT TRUNC(SYSDATE) + 2
           + CASE TO_CHAR(TRUNC(SYSDATE) + 2, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH')
                  WHEN 'SAT' THEN 2 WHEN 'SUN' THEN 1 ELSE 0 END AS D    -- ← 배송일 (RouteOpt Plan date)
    FROM dual
),
st AS (                                -- STAT_T 기준
    SELECT 400 AS CODE, 'UNASSIGNED' AS GRP FROM dual UNION ALL       -- Unassigned Open
    SELECT 410, 'UNASSIGNED' FROM dual UNION ALL                      -- Processed Unattached
    SELECT 415, 'UNASSIGNED' FROM dual UNION ALL                      -- Select Optimization
    SELECT 420, 'UNASSIGNED' FROM dual UNION ALL                      -- Optimizing
    SELECT 425, 'UNASSIGNED' FROM dual UNION ALL                      -- Optimized
    SELECT 430, 'UNASSIGNED' FROM dual UNION ALL                      -- Planned
    SELECT 435, 'UNASSIGNED' FROM dual UNION ALL                      -- Confirming Manifest
    SELECT 300, 'OPEN'       FROM dual                                -- LL OPEN
    -- UNION ALL SELECT 305, 'OPEN' FROM dual                         -- LL PLANNED (포함하려면 주석 해제)
    -- UNION ALL SELECT 315, 'OPEN' FROM dual                         -- LL TENDER REJECTED (포함하려면 주석 해제)
),
svc AS (                               -- 대상 서비스 (GRND·HTL·DFLT 등 제외)
    SELECT 'LFLT' AS S FROM dual UNION ALL SELECT 'LTL'  FROM dual UNION ALL
    SELECT 'LTPR'      FROM dual UNION ALL SELECT 'SPOT' FROM dual UNION ALL
    SELECT 'BTLL'      FROM dual UNION ALL SELECT 'TL'   FROM dual UNION ALL
    SELECT 'PTL'       FROM dual UNION ALL SELECT 'LTEC' FROM dual
),
-- MJ-WEST PoC (v260105)의 CA Ship-to 전체 111곳. LFLT_OK = Y: LFLT? = Y 이고 Compliance? = N (48곳) / N: 그 외 (63곳)
-- PoC에 없는 Ship-to는 이 목록과 무관하게 다른 조건대로 판단. PoC가 바뀌면 이 목록만 교체
poc AS (
    SELECT '004565-BLDR-S'   AS SHIP_TO, 'Y' AS LFLT_OK, 'ANY'         AS REQ_DAYS FROM dual   -- LFLT=Y | R & B WHOLESALE DISTRIBUTORS  INC.
    UNION ALL SELECT '004565-PJT-S'   , 'Y', 'ANY'         FROM dual   -- LFLT=Y | R & B WHOLESALE DISTRIBUTORS  INC.
    UNION ALL SELECT '02052SNY'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | BOEING, CREDIT CARD SALES
    UNION ALL SELECT '04440203-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | A. & A. FEDERS INC.
    UNION ALL SELECT '0466592841HE-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | VEYER LLC #3013
    UNION ALL SELECT '05007010-S'     , 'Y', 'TUE'         FROM dual   -- LFLT=Y | HOWARDS APPLIANCES, INC.
    UNION ALL SELECT '0503-BLDR-S'    , 'Y', 'ANY'         FROM dual   -- LFLT=Y | R&M Appliance Inc (SKS)
    UNION ALL SELECT '06353027-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | MICRO ELECTRONICS, INC. (#101)
    UNION ALL SELECT '072944-S'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | WASH MULTIFAMILY LAUNDRY SYSTEMS, 
    UNION ALL SELECT '193971'         , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '427750CO'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | MA LABORATORIES, INC.
    UNION ALL SELECT '42775109'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | MA LABORATORIES, INC.
    UNION ALL SELECT '435830011HE-S'  , 'Y', 'ANY'         FROM dual   -- LFLT=Y | NEWEGG // MAGNELL ASSOCIATE, INC.
    UNION ALL SELECT '45678016-S'     , 'Y', 'MON,WED,FRI' FROM dual   -- LFLT=Y | UNITED STORES INC.
    UNION ALL SELECT '4574-PJT-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | R & B WHOLESALE DISTRIBUTORS  INC.
    UNION ALL SELECT '46470002-BLDR-S', 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '4648076'        , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '4648564-S'      , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (SKS)
    UNION ALL SELECT '4648601-S'      , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '4648711-BLDR-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '4648717-BLDR-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FERGUSON ENTERPRISES, LLC (BUILDER
    UNION ALL SELECT '46613001-HA-W'  , 'Y', 'ANY'         FROM dual   -- LFLT=Y | URNERS INC
    UNION ALL SELECT '46871001'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | FUTURE TECH ENTERPRISE
    UNION ALL SELECT '5007003-PJT-S'  , 'Y', 'TUE'         FROM dual   -- LFLT=Y | HOWARDS APPLIANCES, INC. (PJT)
    UNION ALL SELECT '5007004-SKS-S'  , 'Y', 'TUE'         FROM dual   -- LFLT=Y | HOWARDS APPLIANCES, INC.
    UNION ALL SELECT '6182001'        , 'Y', 'ANY'         FROM dual   -- LFLT=Y | ADIR INTERNATIONAL, LLC
    UNION ALL SELECT '63216002'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | NEXCEN #720
    UNION ALL SELECT '64415002-PJT-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | PINNACLE APPLIANCE DISTRIBUTORS, I
    UNION ALL SELECT '69098S01-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | TRANSOURCE SERVICES CORP.
    UNION ALL SELECT '73013001-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | AMPRONIX LLC
    UNION ALL SELECT '73223013-BLDR-S', 'Y', 'WED'         FROM dual   -- LFLT=Y | UNIVERSAL APPLIANCES, KITCHENS AND
    UNION ALL SELECT '73223013-PJT-S' , 'Y', 'WED'         FROM dual   -- LFLT=Y | UNIVERSAL APPLIANCES, KITCHENS AND
    UNION ALL SELECT '7417501-BLDR-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | NATIVE COAST APPLIANCE GALLERY INC
    UNION ALL SELECT '74257001-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | MA LABORATORIES, INC.
    UNION ALL SELECT '7514-DROP-S'    , 'Y', 'ANY'         FROM dual   -- LFLT=Y | ABT ELECTRONICS INC (PJT)
    UNION ALL SELECT '898010-BLDR-S'  , 'Y', 'ANY'         FROM dual   -- LFLT=Y | EXPRESSIONS HOME GALLERY
    UNION ALL SELECT '91748CA-S'      , 'Y', 'ANY'         FROM dual   -- LFLT=Y | MA LABORATORIES, INC.
    UNION ALL SELECT 'ALL7865-WH'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | ALLIED REFRIGERATION INC.
    UNION ALL SELECT 'BCAC1220-WH-S'  , 'Y', 'ANY'         FROM dual   -- LFLT=Y | BUFFALO ELECTRIC WHOLESALE, INC.
    UNION ALL SELECT 'BCAC1416-WH-S'  , 'Y', 'ANY'         FROM dual   -- LFLT=Y | BUFFALO ELECTRIC WHOLESALE, INC.
    UNION ALL SELECT 'BDEC899-WH'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | BAKER DISTRIBUTING COMPANY LLC
    UNION ALL SELECT 'BUFF-ESS-S'     , 'Y', 'ANY'         FROM dual   -- LFLT=Y | BUFFALO ELECTRIC WHOLESALE, INC.
    UNION ALL SELECT 'COOLMARTGROUP-S', 'Y', 'ANY'         FROM dual   -- LFLT=Y | COOLMART GROUP, INC.
    UNION ALL SELECT 'DMG-DR-S'       , 'Y', 'ANY'         FROM dual   -- LFLT=Y | CHRISTIAN BROS
    UNION ALL SELECT 'DMG1110-WH-S'   , 'Y', 'ANY'         FROM dual   -- LFLT=Y | DMG HVAC LLC
    UNION ALL SELECT 'DMGCORP-S'      , 'Y', 'ANY'         FROM dual   -- LFLT=Y | DMG CORPORATION
    UNION ALL SELECT 'US006212503B-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | LG AE - CAC (SAMPLE PORTAL)
    UNION ALL SELECT 'US006212509B-S' , 'Y', 'ANY'         FROM dual   -- LFLT=Y | LG HE - B2B (SAMPLE PORTAL)
    UNION ALL SELECT '00176225-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | TARGET #3806
    UNION ALL SELECT '00480692-S'     , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '006899010-S'    , 'N', 'ANY'         FROM dual   -- LFLT=blank | HOME DEPOT PRODUCT AUTHORITY LLC (
    UNION ALL SELECT '006899036-S'    , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC (
    UNION ALL SELECT '0072192021-S'   , 'N', 'ANY'         FROM dual   -- Compliance=Y | WALMART INC. (US eComm FC 7049)
    UNION ALL SELECT '00851201-S'     , 'N', 'ANY'         FROM dual   -- LFLT=blank | L & D APPLIANCE CORP.
    UNION ALL SELECT '04440202-PJT-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | A. & A. FEDERS INC. (PJT)
    UNION ALL SELECT '05686013-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | COSTCO DDC #960
    UNION ALL SELECT '0596187-S'      , 'N', 'ANY'         FROM dual   -- Compliance=Y | COSTCO LOGISTICS#1252
    UNION ALL SELECT '06051030-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | INGRAM MICRO INC.
    UNION ALL SELECT '06618302-S'     , 'N', 'THU'         FROM dual   -- LFLT=blank | T.J.S. ENTERPRISES, INC.
    UNION ALL SELECT '06843CHINO-S'   , 'N', 'ANY'         FROM dual   -- Compliance=Y | TD SYNNEX CORPORATION
    UNION ALL SELECT '06843LCA-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | TD SYNNEX CORPORATION
    UNION ALL SELECT '069993001-S'    , 'N', 'ANY'         FROM dual   -- LFLT=blank | UNITED RADIO INCORPORATED
    UNION ALL SELECT '073036-BLDR-S'  , 'N', 'TUE'         FROM dual   -- LFLT=blank | SNYDER-DIAMOND (BUILDER)
    UNION ALL SELECT '073036-PJT-S'   , 'N', 'TUE'         FROM dual   -- LFLT=blank | SNYDER-DIAMOND
    UNION ALL SELECT '0731000012-S'   , 'N', 'ANY'         FROM dual   -- Compliance=Y | INGRAM MICRO INC.
    UNION ALL SELECT '1175007-BLDR-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | THE LIVART GROUP (PJT)
    UNION ALL SELECT '17270016C-S'    , 'N', 'ANY'         FROM dual   -- Compliance=Y | BEST BUY PURCHASING LLC
    UNION ALL SELECT '17270030-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | BEST BUY PURCHASING LLC #2605
    UNION ALL SELECT '17270030C-S'    , 'N', 'ANY'         FROM dual   -- Compliance=Y | BEST BUY PURCHASING LLC #2605
    UNION ALL SELECT '176202'         , 'N', 'ANY'         FROM dual   -- Compliance=Y | TARGET #0553
    UNION ALL SELECT '195658-S'       , 'N', 'ANY'         FROM dual   -- Compliance=Y | LOWES COMPANIES, INC. (SOS)
    UNION ALL SELECT '20018210'       , 'N', 'ANY'         FROM dual   -- Compliance=Y | INGRAM MICRO INC.
    UNION ALL SELECT '3022-BLDR-S'    , 'N', 'ANY'         FROM dual   -- LFLT=blank | LOWES HOME CENTERS, LLC (Builder)
    UNION ALL SELECT '4003-PJT-S'     , 'N', 'ANY'         FROM dual   -- LFLT=blank | SNYDER-DIAMOND-Oceanside
    UNION ALL SELECT '43103019'       , 'N', 'ANY'         FROM dual   -- LFLT=blank | ASI COMPUTER TECHNOLOGIES, INC.
    UNION ALL SELECT '44698001'       , 'N', 'ANY'         FROM dual   -- LFLT=blank | VIDEO & AUDIO CENTER
    UNION ALL SELECT '45678024-HA'    , 'N', 'ANY'         FROM dual   -- LFLT=blank | UNITED STORES INC.
    UNION ALL SELECT '45866028'       , 'N', 'ANY'         FROM dual   -- Compliance=Y | IT - SAMS DISTRIBUTION CENTER #649
    UNION ALL SELECT '4587560-S'      , 'N', 'ANY'         FROM dual   -- Compliance=Y | SAMS EDC #8799
    UNION ALL SELECT '477293'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477295'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477297'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477323'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477325'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477331'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '477776'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '480431'         , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC
    UNION ALL SELECT '49932801-S'     , 'N', 'ANY'         FROM dual   -- LFLT=blank | ALMO CORPORATION
    UNION ALL SELECT '5686003'        , 'N', 'ANY'         FROM dual   -- Compliance=Y | COSTCO DDC #960
    UNION ALL SELECT '5686004-S'      , 'N', 'ANY'         FROM dual   -- LFLT=N | COSTCO VMI #1252
    UNION ALL SELECT '56860787-S'     , 'N', 'ANY'         FROM dual   -- Compliance=Y | COSTCO LOGISTICS#1252
    UNION ALL SELECT '56860884-D-S'   , 'N', 'ANY'         FROM dual   -- Compliance=Y | COSTCO LOGISTICS#1252
    UNION ALL SELECT '6051001'        , 'N', 'ANY'         FROM dual   -- Compliance=Y | INGRAM MICRO INC.
    UNION ALL SELECT '64415005-PJT-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | PINNACLE APPLIANCE DISTRIBUTORS, I
    UNION ALL SELECT '72190989'       , 'N', 'ANY'         FROM dual   -- LFLT=blank | WAL-MART DC # 7033T - TAB
    UNION ALL SELECT '7219921875-S'   , 'N', 'ANY'         FROM dual   -- Compliance=Y | WALMART INC. (US eComm FC 7049)
    UNION ALL SELECT '73223011-PJT-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | UNIVERSAL APPLIANCES, KITCHENS AND
    UNION ALL SELECT '73223011-SKS-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | UNIVERSAL APPLIANCES, KITCHENS AND
    UNION ALL SELECT '7353911-DROP-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | DEVELOPERS DIRECT LLC
    UNION ALL SELECT '74066001-HA-W'  , 'N', 'ANY'         FROM dual   -- LFLT=blank | REIDS, INC.
    UNION ALL SELECT '8303-SKS-S'     , 'N', 'THU'         FROM dual   -- LFLT=blank | T.J.S. ENTERPRISES, INC.
    UNION ALL SELECT '950207'         , 'N', 'ANY'         FROM dual   -- Compliance=Y | BEST BUY PURCHASING LLC
    UNION ALL SELECT 'ALL306-WH'      , 'N', 'ANY'         FROM dual   -- LFLT=blank | ALLIED REFRIGERATION INC.
    UNION ALL SELECT 'ALRE-CAC-4-S'   , 'N', 'ANY'         FROM dual   -- LFLT=blank | ALLIED REFRIGERATION INC.
    UNION ALL SELECT 'BD241-WH'       , 'N', 'ANY'         FROM dual   -- LFLT=blank | BAKER DISTRIBUTING COMPANY LLC
    UNION ALL SELECT 'DMG301-WH-S'    , 'N', 'ANY'         FROM dual   -- LFLT=blank | DMG HVAC LLC
    UNION ALL SELECT 'DROPSHIP-PJT-S' , 'N', 'ANY'         FROM dual   -- LFLT=N | HOME DEPOT PRODUCT AUTHORITY LLC (
    UNION ALL SELECT 'MV-DMGCORP-S'   , 'N', 'ANY'         FROM dual   -- LFLT=blank | DMG CORPORATION
    UNION ALL SELECT 'STOFC-692-S'    , 'N', 'ANY'         FROM dual   -- Compliance=Y | STAPLES #692
    UNION ALL SELECT 'US000120-S'     , 'N', 'ANY'         FROM dual   -- LFLT=blank | CLOVER ENVIRONMENTAL SOLUTIONS LLC
    UNION ALL SELECT 'US006212506B-S' , 'N', 'ANY'         FROM dual   -- LFLT=blank | LG HA (SAMPLE PORTAL)
),
-- 1) 읽는 범위: 기간 제한 없음. 대신 지금 미배정이거나 Open 로드에 실린 Shipment만 먼저 골라서 읽음
--    (전체 이력을 다 읽지 않도록. TMS 미등록 건은 어차피 제외 대상이라 결과는 같음)
--    상태코드는 st 와 같은 값을 숫자로 직접 적어야 인덱스(상태코드가 첫 컬럼)를 탐 → st 를 바꾸면 여기도 같이 바꿀 것
live AS (
    SELECT /*+ MATERIALIZE INDEX(t LDDETL_I1632) */ t.SHPM_NUM
    FROM   TMS_PROD.LD_LEG_DETL_T t
    WHERE  t.CUR_OPTLSTAT_ID IN (400, 410, 415, 420, 425, 430, 435)                    -- 미배정
    UNION
    SELECT /*+ INDEX(a LD_LEG_I11) USE_NL(t) INDEX(t LD_LEG_DETL_I11) */ t.SHPM_NUM
    FROM   TMS_PROD.LD_LEG_T a
    JOIN   TMS_PROD.LD_LEG_DETL_T t ON t.LD_LEG_ID = a.LD_LEG_ID
    WHERE  a.CUR_OPTLSTAT_ID = 300                                                    -- Open 로드
      AND  a.FRST_SHPG_LOC_CD IN ('LGC2', 'LGC7', 'LGC8')
),
c AS (
    SELECT /*+ MATERIALIZE */ x.*
    FROM   TMS_IF.IF_IN_SHIPMENT x
    WHERE  x.SHIPFROMLOCATIONCODE IN ('LGC2', 'LGC7', 'LGC8')
      AND  x.SHIPMENTNUMBER IN (SELECT SHPM_NUM FROM live)
      AND  NVL(x.LMD_FG, 'N')    = 'N'
      AND  NVL(x.CANCEL_YN, 'N') = 'N'
),
k AS (
    SELECT /*+ MATERIALIZE INDEX(d IX_TB_KPI_CONTAINER_02) */ d.SHPM_NUM, d.HQ, d.RDD_FROM, d.RDD_TO, d.ALLOCATED_COST,
           ROW_NUMBER() OVER (PARTITION BY d.SHPM_NUM ORDER BY d.UPDATE_DT DESC NULLS LAST) rn
    FROM   TMS_IF.TB_KPI_CONTAINER_02 d
    WHERE  d.SHPM_NUM IN (SELECT SHIPMENTNUMBER FROM c)
),
sv AS (                                -- Shipment 단위 Declared value, Bill-to
    SELECT /*+ MATERIALIZE */ s.SHPM_NUM, MAX(s.DCLD_VAL_DLR) AS DCLD_VAL, MAX(s.BILLTO_CUST_CD) AS BILLTO
    FROM   TMS_PROD.SHPM_T s
    WHERE  s.SHPM_NUM IN (SELECT SHIPMENTNUMBER FROM c)
    GROUP  BY s.SHPM_NUM
),
-- 2) 구간이 2개인 Shipment는 우리 DC에서 출발하는 구간을 우선
b AS (
    SELECT /*+ MATERIALIZE INDEX(t LD_LEG_DETL_I8) */ t.SHPM_NUM, t.LD_LEG_ID, t.SRVC_CD, t.CUR_OPTLSTAT_ID, t.TO_SHPG_LOC_CD,
           t.CUST_NAME, t.TO_SHPG_LOC_NAME, t.TO_STA_CD, t.TO_CTY_NAME, t.TO_PSTL_CD, t.PLAN_ID,
           ROW_NUMBER() OVER (PARTITION BY t.SHPM_NUM
                              ORDER BY CASE WHEN t.FRM_SHPG_LOC_CD IN ('LGC2', 'LGC7', 'LGC8') THEN 0 ELSE 1 END,
                                       t.UPDT_DTT DESC NULLS LAST, t.LD_LEG_ID DESC NULLS LAST) rn
    FROM   TMS_PROD.LD_LEG_DETL_T t
    WHERE  t.SHPM_NUM IN (SELECT SHIPMENTNUMBER FROM c)
),
u AS (
    SELECT /*+ MATERIALIZE USE_NL(a) INDEX(a PK_LDLEG) */ c.SHIPMENTNUMBER                                     AS SHPM_NUM,
           c.PO,
           a.LD_LEG_ID,
           a.SRVC_CD                                            AS LD_SRVC_CD,
           a.DCLD_VAL_DLR                                       AS LD_DCLD_VAL, -- 로드 단위 Declared value
           a.EXTL_URL2                                          AS EXT_URL2,    -- External URL2 (REQ = appointment 요청함)
           TRUNC(a.STRD_DTT)                                    AS LOAD_DATE,   -- 로드 출발 예정일 (참고용)
           COALESCE(a.SRVC_CD, b.SRVC_CD, c.SERVICECODE)        AS SRVC_CD,
           NVL(a.CUR_OPTLSTAT_ID, b.CUR_OPTLSTAT_ID)            AS STATUS,
           b.CUR_OPTLSTAT_ID                                    AS LEG_STATUS,
           CASE WHEN b.SHPM_NUM IS NULL      THEN 'A_NOT_IN_TMS'      -- TMS 미등록 (출고·삭제 건)
                WHEN sb.GRP = 'UNASSIGNED'   THEN 'A_UNASSIGNED'      -- 미배정 (400번대)
                ELSE 'B_OPEN' END                               AS BUCKET,
           c.SHIPFROMLOCATIONCODE                               AS DC,
           NVL(c.SHIPTOLOCATIONCODE, b.TO_SHPG_LOC_CD)          AS TO_CD,
           COALESCE(c.SHIPTODESCRIPTION, b.CUST_NAME, c.CUSTOMER_DESC) AS CUST_NAME,
           b.TO_SHPG_LOC_NAME                                   AS STOP_NAME,   -- Stop Location Name (받는 곳 이름)
           UPPER(c.SHIPTODESCRIPTION || ' ' || b.CUST_NAME || ' ' || c.CUSTOMER_DESC) AS ALL_NAMES,
           TRIM(c.STREET)                                       AS ORIG_ADDR,
           UPPER(REGEXP_REPLACE(TRIM(c.STREET), '\s+', ' '))    AS RAW_ADDR,
           TRIM(NVL(c.CITY,  b.TO_CTY_NAME))                    AS CITY,
           NVL(c.STATE, b.TO_STA_CD)                            AS STATE,
           SUBSTR(LPAD(NVL(c.POSTALCODE, b.TO_PSTL_CD), 5, '0'), 1, 5) AS ZIP5,
           c.CONSIGNEE_TEL1                                     AS PHONE,
           SUBSTR(TRIM(c.CONTACT_POINT
                  || CASE WHEN c.PRINTABLEMEMO <> NVL(c.CONTACT_POINT, '#') THEN ' ' || c.PRINTABLEMEMO END
                  || CASE WHEN c.NONPRINTABLEMEMO <> NVL(c.PRINTABLEMEMO, '#')
                           AND c.NONPRINTABLEMEMO <> NVL(c.CONTACT_POINT, '#') THEN ' ' || c.NONPRINTABLEMEMO END), 1, 1000) AS MEMO,
           UPPER(c.CONTACT_POINT || ' ' || c.PRINTABLEMEMO || ' ' || c.NONPRINTABLEMEMO) AS MEMO_UP,
           c.MODEL_CODE,
           c.PIECES,
           c.WGT * NVL(c.PIECES, 1)                             AS WGT,      -- WGT는 1개당 → 수량 곱함
           c.VOLUME * NVL(c.PIECES, 1)                          AS VOL,      -- VOLUME은 1개당 → 수량 곱함
           NVL(k.HQ, CASE WHEN c.GBU IN ('DFT', 'CNT', 'CVT', 'CDT', 'DVT') THEN 'HA'
                          WHEN c.GBU IN ('GTT', 'GLT', 'PNT', 'PCT', 'PST') THEN 'HE'
                          WHEN c.GBU IN ('DGT', 'DMT', 'DLT', 'DTT', 'SRT') THEN 'AC'
                          WHEN c.GBU IN ('GJT', 'GNT', 'GKT')               THEN 'BS'
                     END)                                       AS HQ,       -- KPI에 없으면 GBU로 추정
           k.ALLOCATED_COST,
           NVL(sv.DCLD_VAL, 0)                                  AS DCLD_VAL,    -- Shipment Declared value
           COALESCE(sv.BILLTO, c.BILLTOCUSTOMERCODE)            AS BILLTO,      -- Bill-to (TMS, 없으면 IF)
           TRIM(COALESCE(TO_CHAR(b.PLAN_ID), TO_CHAR(c.SYSTEMPLANID))) AS PLAN_ID,  -- Plan ID (예: 7774)
           TO_NUMBER(TRIM(c.PLAN_ID_GRP))                       AS PLAN_ID_GRP,
           c.PLAN_ID_TYP,
           TRUNC(NVL(k.RDD_FROM, c.DELIVERYFROMDATETIME))       AS RDD_FROM,
           TRUNC(NVL(k.RDD_TO,   c.DELIVERYTODATETIME))         AS RDD_TO
    FROM   c
    LEFT   JOIN b                   ON b.SHPM_NUM  = c.SHIPMENTNUMBER AND b.rn = 1
    LEFT   JOIN TMS_PROD.LD_LEG_T a ON a.LD_LEG_ID = b.LD_LEG_ID
    LEFT   JOIN st sb ON sb.CODE = b.CUR_OPTLSTAT_ID AND sb.GRP = 'UNASSIGNED'
    LEFT   JOIN st sa ON sa.CODE = a.CUR_OPTLSTAT_ID AND sa.GRP = 'OPEN'
    LEFT   JOIN k     ON k.SHPM_NUM = c.SHIPMENTNUMBER AND k.rn = 1
    LEFT   JOIN sv    ON sv.SHPM_NUM = c.SHIPMENTNUMBER
    WHERE  b.SHPM_NUM IS NULL                                             -- TMS 미등록 (사유 표시 후 제외)
       OR  sb.GRP = 'UNASSIGNED'                                          -- 미배정
       OR  (sa.GRP = 'OPEN' AND (a.SRVC_CD IN (SELECT S FROM svc)         -- Open 로드
                                 OR a.SRVC_CD IS NULL))                   --   서비스 미정 Open 로드 포함
),
-- 3) 주소 표준화 ①: 도로 종류(ROAD, AVE …) 뒤의 건물·도어·매장번호 등을 잘라냄
na AS (
    SELECT x.*,
           CASE WHEN x.P1 > 0 AND x.P1 <= LENGTH(x.RAW_ADDR)
                 AND REGEXP_INSTR(x.RAW_ADDR,
                       '\s(AVE|AVENUE|ST|STREET|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|WAY|LN|LANE|CT|COURT|PL|PLACE|PKWY|PARKWAY|HWY|HIGHWAY|CIR|CIRCLE|TER|TERRACE|TRL|TRAIL|LOOP|EXPY|FWY)\.?(\s|,|$)',
                       x.P1) > 0
                THEN RTRIM(SUBSTR(x.RAW_ADDR, 1,
                       REGEXP_INSTR(x.RAW_ADDR,
                         '\s(AVE|AVENUE|ST|STREET|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|WAY|LN|LANE|CT|COURT|PL|PLACE|PKWY|PARKWAY|HWY|HIGHWAY|CIR|CIRCLE|TER|TERRACE|TRL|TRAIL|LOOP|EXPY|FWY)\.?(\s|,|$)',
                         x.P1, 1, 1) - 1), ' ,.')
                ELSE TRIM(REGEXP_REPLACE(x.RAW_ADDR,
                       '(\s*,|\s*#|\sBLDG|\sBUILDING|\sBUIDLING|\sDOOR|\sDOCK|\sSUITE|\sSTE|\sUNIT|\sAPT).*$', ''))
           END                                                  AS ADDR_CUT
    FROM  (SELECT u.*, REGEXP_INSTR(u.RAW_ADDR, '^[0-9]+[A-Z]?\s+\S+', 1, 1, 1) AS P1 FROM u) x
),
-- 4) 주소 표준화 ②: 마침표·쉼표 제거, 공백 정리, 약어 통일 → RouteOpt가 같은 스톱으로 묶음
ad AS (
    SELECT na.*,
           TRIM(
           REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
           REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
           REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
           REGEXP_REPLACE(REGEXP_REPLACE(
             REGEXP_REPLACE(REGEXP_REPLACE(NVL(na.ADDR_CUT, na.RAW_ADDR), '[.,]', ''), '\s+', ' '),
           '(^|\s)AVENUE(\s|$)',    '\1AVE\2'),  '(^|\s)STREET(\s|$)',  '\1ST\2'),
           '(^|\s)ROAD(\s|$)',      '\1RD\2'),   '(^|\s)DRIVE(\s|$)',   '\1DR\2'),
           '(^|\s)BOULEVARD(\s|$)', '\1BLVD\2'), '(^|\s)PARKWAY(\s|$)', '\1PKWY\2'),
           '(^|\s)HIGHWAY(\s|$)',   '\1HWY\2'),  '(^|\s)LANE(\s|$)',    '\1LN\2'),
           '(^|\s)COURT(\s|$)',     '\1CT\2'),   '(^|\s)PLACE(\s|$)',   '\1PL\2'),
           '(^|\s)NORTH(\s|$)',     '\1N\2'),    '(^|\s)SOUTH(\s|$)',   '\1S\2'),
           '(^|\s)EAST(\s|$)',      '\1E\2'),    '(^|\s)WEST(\s|$)',    '\1W\2'))  AS ADDR
    FROM   na
),
-- 5) 배송일은 모두 D. PoC Ship-to 매칭(앞자리 0 무시). 팔레트 판정
g AS (
    SELECT /*+ MATERIALIZE */ ad.*,
           poc.SHIP_TO                                          AS POC_SHIP_TO,
           poc.LFLT_OK                                          AS POC_LFLT_OK,   -- NULL = PoC에 없는 Ship-to
           NVL(poc.REQ_DAYS, 'ANY')                             AS REQ_DAYS,
           CASE WHEN ad.BUCKET = 'B_OPEN' AND ad.LD_SRVC_CD = 'LFLT' THEN 'Y' ELSE 'N' END AS IS_LFLT_LOAD,
           CASE WHEN NVL(ad.HQ, 'X') <> 'HA' THEN 'PRODUCT'                         -- HA 외 제품군
                WHEN REGEXP_LIKE(ad.MEMO_UP, 'PALLETI[ZS]')
                 AND NOT REGEXP_LIKE(ad.MEMO_UP, '(NO|NOT|DONT|DON''T)\s+(BE\s+)?PALLETI[ZS]') THEN 'MEMO'  -- HA 예외
           END                                                  AS PALLET_REASON,
           p.D                                                  AS DLV_DATE,
           CASE WHEN ad.RDD_FROM IS NULL THEN NULL
                WHEN p.D < ad.RDD_FROM   THEN 'EARLY'
                WHEN p.D > ad.RDD_TO     THEN 'LATE'
                ELSE 'IN_RDD' END                               AS RDD_TIMING,
           CASE WHEN p.D < ad.RDD_FROM THEN ad.RDD_FROM - p.D
                WHEN p.D > ad.RDD_TO   THEN p.D - ad.RDD_TO
                ELSE 0 END                                      AS RDD_GAP_DAYS
    FROM   ad
    CROSS  JOIN p
    LEFT   JOIN poc ON LTRIM(UPPER(TRIM(ad.TO_CD)), '0') = LTRIM(UPPER(poc.SHIP_TO), '0')
),
-- 6) 과거 6개월 직배송(NUM_STOP = 2) 로드의 ZIP별 편도 마일 중앙값 (참고용, 이상값에 덜 흔들림)
dist AS (
    SELECT SUBSTR(LPAD(l.LAST_PSTL_CD, 5, '0'), 1, 5) AS ZIP5,
           ROUND(MEDIAN(l.FIXD_ITNR_DIST), 1)         AS MED_MILES,
           COUNT(*)                                   AS HIST_LOADS
    FROM   TMS_PROD.LD_LEG_T l
    WHERE  l.FRST_SHPG_LOC_CD IN ('LGC2', 'LGC7', 'LGC8')
      AND  l.SHPD_DTT >= ADD_MONTHS(TRUNC(SYSDATE), -6)
      AND  l.FIXD_ITNR_DIST > 0
      AND  l.NUM_STOP = 2
      AND  SUBSTR(LPAD(l.LAST_PSTL_CD, 5, '0'), 1, 5) IN (SELECT ZIP5 FROM g)
    GROUP  BY SUBSTR(LPAD(l.LAST_PSTL_CD, 5, '0'), 1, 5)
),
-- 7) 볼륨 외 제외 사유 + 스톱 묶음 키 (같은 출발 DC → 같은 표준화 주소 + ZIP)
e AS (
    SELECT g.*, dist.MED_MILES, dist.HIST_LOADS,
           CASE
             WHEN g.BUCKET = 'A_NOT_IN_TMS'                                       THEN 'NOT_IN_TMS'
             WHEN REGEXP_LIKE(UPPER(g.EXT_URL2), '(^|[^A-Z])REQ([^A-Z]|$)')       THEN 'APPT_REQUESTED'  -- 요청한 로드
             WHEN g.ADDR IS NULL OR g.CITY IS NULL OR g.ZIP5 IS NULL              THEN 'NO_ADDRESS'
             WHEN NVL(g.STATE, 'X') NOT IN ('CA', 'NV', 'AZ')                     THEN 'OUT_OF_AREA'
             WHEN g.PLAN_ID = '7774'                                              THEN 'OK'          -- Plan 7774: 나머지 조건 무시
             WHEN REGEXP_LIKE(g.ALL_NAMES, '(^|[^A-Z])OBS([^A-Z]|$)')             THEN 'OBS'
             WHEN REGEXP_LIKE(g.ALL_NAMES, 'BEST\s*BUY')                          THEN 'BEST_BUY'    -- LFLT 불가 고객
             WHEN UPPER(g.PLAN_ID_TYP) LIKE '%COMPLIANCE%'                         THEN 'COMPLIANCE'
             WHEN UPPER(TRIM(g.PLAN_ID_TYP)) = 'DEDICATED_INTERNAL'                THEN 'DEDICATED_INTERNAL'
             WHEN g.POC_LFLT_OK = 'N'                                             THEN 'POC_LFLT_N'   -- PoC에 있는데 LFLT? = Y 아님
             WHEN g.SRVC_CD IS NOT NULL AND g.SRVC_CD NOT IN (SELECT S FROM svc) THEN 'SRVC_EXCLUDED'
             -- 기존 LFLT Open 로드는 홀드 중인 로드라 날짜·PG와 관계없이 포함. RDD는 모든 PG에서 판단에 안 씀
             WHEN g.IS_LFLT_LOAD = 'N'
              AND (g.PLAN_ID_GRP IS NULL OR g.PLAN_ID_GRP NOT IN (12, 20, 30, 50, 80)) THEN 'PLAN_EXCLUDED'
             -- 입고 요일이 맞지 않으면 제외하려면 아래 두 줄 주석 해제 (지금은 REQ_DAY_FLAG 로 표시만)
             -- WHEN g.REQ_DAYS <> 'ANY'
             --  AND INSTR(g.REQ_DAYS, TO_CHAR(p.D, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH')) = 0 THEN 'REQ_DAY_MISMATCH'
             WHEN g.ADDR IS NULL OR g.CITY IS NULL OR g.ZIP5 IS NULL              THEN 'NO_ADDRESS'
             WHEN NVL(g.STATE, 'X') NOT IN ('CA', 'NV', 'AZ')                     THEN 'OUT_OF_AREA'
             WHEN dist.MED_MILES >= 200                                           THEN 'DIST_200_PLUS'
             ELSE 'OK'
           END                                                                    AS REASON0,
           g.DC || '|' || g.ADDR || '|' || g.ZIP5                                 AS GRP_KEY      -- 미배정·Open 공통
    FROM   g
    CROSS  JOIN p
    LEFT   JOIN dist ON dist.ZIP5 = g.ZIP5
),
-- 8) 스톱 단위 볼륨: 후보 Shipment 합계. 팔레트 건이 하나라도 있으면 400 이하, 없으면 800 이하
h AS (
    SELECT e.*,
           SUM(e.VOL) OVER (PARTITION BY e.REASON0, e.GRP_KEY)                   AS GRP_VOL,
           CASE WHEN MAX(CASE WHEN e.PALLET_REASON IS NOT NULL THEN 1 ELSE 0 END)
                         OVER (PARTITION BY e.REASON0, e.GRP_KEY) = 1
                THEN 400 ELSE 800 END                                             AS GRP_LIMIT,
           COUNT(*) OVER (PARTITION BY e.REASON0, e.GRP_KEY)                     AS GRP_SHPM_CNT,
           DENSE_RANK() OVER (ORDER BY e.GRP_KEY)                                 AS GRP_ID
    FROM   e
),
r AS (
    SELECT h.*,
           CASE WHEN h.REASON0 <> 'OK'           THEN h.REASON0
                WHEN h.GRP_VOL > h.GRP_LIMIT
                 AND NVL(h.PLAN_ID, '-') <> '7774' THEN 'VOL_OVER_' || h.GRP_LIMIT   -- Plan 7774 는 볼륨 한도도 무시
                ELSE 'OK' END                                                     AS REASON
    FROM   h
)
SELECT r.SHPM_NUM                                              AS "shipment_id",
       r.PO                                                    AS "purchase_order",
       r.DLV_DATE                                              AS "delivery_date",
       'LTL'                                                   AS "order_type",
       NVL(r.CUST_NAME, r.TO_CD)                               AS "customer_name",
       r.PHONE                                                 AS "phone",
       r.ADDR                                                  AS "address",       -- 표준화된 주소 (같은 위치 = 같은 스톱)
       r.CITY                                                  AS "city",
       r.STATE                                                 AS "state",
       r.ZIP5                                                  AS "zip",
       r.MODEL_CODE                                            AS "model_code",
       r.PIECES                                                AS "pieces",
       ROUND(r.WGT, 1)                                         AS "weight_lb",
       ROUND(r.VOL, 2)                                         AS "volume_cuft",
       r.DC                                                    AS "origin_dc",
       CASE WHEN r.PALLET_REASON IS NOT NULL THEN 'Y' ELSE 'N' END AS "pallet_required",
       CASE WHEN r.IS_LFLT_LOAD = 'Y' THEN 1 ELSE 2 END        AS "priority",
       r.MEMO                                                  AS "memo",
       CASE WHEN LENGTH(NVL(r.ADDR_CUT, r.RAW_ADDR)) < LENGTH(r.RAW_ADDR)
            THEN 'Address detail: ' || r.ORIG_ADDR END         AS "notes",         -- 잘라낸 건물·도어 정보
       ROUND(r.DCLD_VAL, 2)                                    AS "declared_value", -- SHPM_T Shipment 값
       r.BILLTO                                                AS "bill_to_code",   -- Appointments 화면에 표시
       -- 참고 컬럼 (RouteOpt는 무시함)
       r.STOP_NAME                                              AS STOP_NAME,       -- 도착지 이름 (TMS Stop Location Name)
       CASE WHEN REGEXP_LIKE(UPPER(r.STOP_NAME), '(\s|^)(INC|LLC|CO|CORP|CORPORATION|COMPANY|LTD|LP|GROUP|SUPPLY|APPLIANCES?|DISTRIB\w*|WHOLESALE|WAREHOUSE|WHSE|MDO|3PL|DC|DDC|RDC|ADC|BDC|HUB|STORES?|CENTER|CENTRE|DEPOT|SERVICES?|ELECTRIC\w*|HVAC|AIR|HEATING|PLUMBING|HOME|HOMES|FURNITURE|LOGISTICS|TERMINAL|BUILDERS?|CONSTRUCTION|DEVELOPMENT|KITCHENS?|BATHS?|DESIGN|GALLERY|INTERNATIONAL|ENTERPRISES?|SOLUTIONS|TECHNOLOG\w*|SYSTEMS?|PRODUCTS?|INDUSTRIES|ASSOCIATES|PARTNERS|HQ|CITY|COUNTY|SCHOOL|HOSPITAL|CHURCH|FINANCIAL|ADMIN|BANK|INSURANCE|CAPITAL|HOLDINGS|PROPERTIES|REALTY|MANAGEMENT|MGMT|OFFICE|STUDIO|CLINIC|DENTAL|LAW|SALON|RESTAURANT|CAFE|HOTEL|APARTMENTS?|CONDOS?|HOA)(\s|$)')
              OR REGEXP_LIKE(r.STOP_NAME, '[0-9#&]')                               THEN 'N'      -- 업체·물류센터 이름
            WHEN REGEXP_LIKE(TRIM(r.STOP_NAME), '^[A-Za-z''.-]+( [A-Za-z''.-]+){1,2}$') THEN 'Y'      -- 사람 이름 (2~3단어)
            WHEN UPPER(r.TO_CD) LIKE '%DROP%'
              OR REGEXP_LIKE(r.RAW_ADDR, '(\s(APT|UNIT)\b|#)')                     THEN 'MAYBE'  -- 개인배송용 Ship-to 코드 / 호수 표시
            ELSE 'N' END                                        AS RESIDENTIAL,     -- Y 주거지(사람 이름) / MAYBE 추정 / N
       r.ORIG_ADDR, r.HQ, r.PALLET_REASON,
       CASE WHEN r.POC_LFLT_OK = 'Y' THEN 'PoC Y' ELSE 'Not in PoC' END AS POC_STATUS, r.REQ_DAYS,
       CASE WHEN r.REQ_DAYS <> 'ANY'
             AND INSTR(r.REQ_DAYS, TO_CHAR(r.DLV_DATE, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH')) = 0
            THEN 'REQ_DAY_MISMATCH' END                         AS REQ_DAY_FLAG,
       r.RDD_TIMING, r.RDD_GAP_DAYS, r.RDD_FROM, r.RDD_TO, r.PLAN_ID, r.PLAN_ID_GRP, r.PLAN_ID_TYP,
       r.LOAD_DATE,
       CASE WHEN r.BUCKET = 'B_OPEN' AND r.LOAD_DATE IS NULL THEN 'LOAD_DATE_UNSET'
            WHEN r.IS_LFLT_LOAD = 'Y' AND r.LOAD_DATE <> r.DLV_DATE THEN 'LOAD_DATE_OTHER_DAY' END AS LOAD_DATE_FLAG,
       r.GRP_ID, r.GRP_SHPM_CNT, ROUND(r.GRP_VOL, 2) AS GRP_VOL, r.GRP_LIMIT,
       r.LD_LEG_ID, r.EXT_URL2, r.LD_DCLD_VAL, r.BUCKET, r.STATUS, r.LEG_STATUS, r.LD_SRVC_CD, r.SRVC_CD, r.TO_CD,
       r.MED_MILES, r.HIST_LOADS,
       CASE WHEN r.MED_MILES >= 150 AND r.MED_MILES < 200 THEN 'CARRIER_CHECK' END AS DIST_FLAG,
       ROUND(r.ALLOCATED_COST, 2) AS ALLOCATED_COST, r.REASON
FROM   r
WHERE  r.REASON = 'OK'                       -- 제외된 건도 보려면 이 줄을 주석 처리
--  AND NVL(r.RDD_TIMING, 'IN_RDD') <> 'EARLY'   -- 조기 배송 제외 시 주석 해제
ORDER  BY r.GRP_ID, r.RDD_FROM, r.SHPM_NUM
FETCH  FIRST 2000 ROWS ONLY
