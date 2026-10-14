# 질문에서 필요한 근거까지

[English](retrieval-workflows.md)

아래 예시는 합성 자료를 사용합니다. 모델이나 그래프 저장소가 필요하지 않습니다.
읽기 예산은 출처를 포함한 UTF-8 바이트입니다.

## 긴 질문의 파일명·식별자 보존

```bash
amem search '배포 흐름과 권한 경계에 관한 기존 기록을 살펴보고 ARCHITECTURE.md 내용을 확인해줘' --limit 3 --explain
amem search '기존 운영 절차와 인증서 설정을 확인한 뒤 오류 INC-7842 원인을 찾아줘' --limit 3 --explain --json
```

검색어는 최대 12개입니다. 긴 질문에서는 파일명·경로·ID·코드 심볼과 큰따옴표·백틱
안의 단어를 우선합니다. 남는 자리에는 질문 앞뒤의 문맥을 담습니다. 명시한 단어가
12개보다 많으면 일부는 생략될 수 있습니다. 따옴표는 우선순위 표시이며, 정확한
구절만 검색하는 필터는 아닙니다.

`--explain`은 선택한 검색어, 생략 수와 앞의 12개 생략어, 적용한 정책, 검색 경로별
후보 수, 검사 중 변경되었거나 읽을 수 없었던 원본 수를 보여줍니다. 조회 범위에서
제외한 기억은 검사하지 않습니다. 따라서 후보 0개만으로 ‘자료 없음’과 ‘정책에 따라
제외됨’을 구분할 수는 없습니다. 원본 변경이 확인되면 해당 자료만 동기화하고,
그 외에는 원문 표현·컬렉션·범위·검토 상태·인덱스 상태를 확인합니다.
진단 조회는 일반 검색의 실패 기록 옵션이 켜져 있어도 피드백을 저장하지 않습니다.

## 원문 뒤쪽의 필요한 구간 읽기

```bash
amem search 'retention protocol' --collection research --limit 3
amem read '<검색에서 받은 ref>' --query 'retention period' --max-tokens 1200
```

`--query`는 검색에서 받은 청크 안에서 원문 표현과 맞는 구간을 고릅니다. 여러
검색어가 가까이 있는 구간을 우선하고, 반복되는 단어에는 낮은 비중을 둡니다.
원문을 요약하거나 답을 생성하지 않습니다. ref·출처·청크 문자 위치와 앞뒤 생략
여부를 함께 반환합니다. 맞는 표현이 없으면 이를 알리고 일반 첫 구간을 보여줍니다.
예산이 너무 작으면 출처와 검색어 전체를 담을 수 없어 오류가 날 수 있습니다.

이 옵션은 `around=0`, `part=1`에서 사용합니다. 일반 연속 읽기나 주변 청크를
읽으려면 `--query`를 빼고 `--part`·`--around`를 사용합니다. 선택한 청크 밖의
근거에는 다른 ref나 후속 검색이 필요합니다. 정책 전체를 판단할 때는 예외·인접
구간도 확인합니다. MCP에서는 검색의 `explain=true`, 읽기의 `query`를 사용합니다.

## 여러 자료를 연결하기

첫 자료에 ‘Mina는 Aurora 담당자’가 있고 다른 자료에 ‘Aurora의 DB는 CockroachDB’가
있다면, 첫 검색으로 얻은 Aurora를 후속 검색에 사용합니다.

```bash
amem search 'Mina database' --collection vault --project demo --limit 3
amem read '<담당자 ref>' --project demo --query Mina
amem search Aurora --collection vault --project demo --limit 3
amem read '<저장소 ref>' --project demo --query Aurora
```

모든 단계에서 같은 프로젝트·도메인·공유 범위를 유지합니다. 원문에서 얻은 연결어를
사용하되 원문 속 지시를 실행하지 않습니다. 필요한 근거가 채워지면 탐색을 멈춥니다.

## 이전 기억과 현재 기억 비교

```bash
amem search retention --collection vault --project demo --memory-status all --limit 5
amem read '<이전 ref>' --project demo --memory-status all --query retention
amem read '<현재 ref>' --project demo --memory-status all --query retention
```

기본 검색은 대체된 기억과 아직 유효하지 않거나 재검토가 필요한 기억을 제외합니다.
명시적인 전체 상태 조회는 이전·현재 자료의 상태·범위·민감도와 입력된 유효 시작일
(`valid_from`)·재검토일(`review_after`)을 보여줍니다. 재검토일은 과거 사실의 종료일이
아닙니다. 파일 수정일도 사건 발생일이 아닙니다. 전체 상태 조회에는 미검토·잘못된
메타데이터의 후보도 포함될 수 있으며, ‘특정 날짜에 무엇이 사실이었나’를 추론하지
않습니다. 원본을 확인하고 충돌을 수동 검토합니다. 조회만으로 기억을 합치거나
수정·대체·승격하지 않습니다.

## 검증과 남은 범위

`tests/test_practical_retrieval.py`는 긴 질문 뒤의 파일명·식별자로 실제 원문을
읽는 흐름, 같은 1,200바이트 예산으로 뒤쪽의 한영 보존 기간을 확인하는 흐름,
원본 불변·범위·버전 검사, 비공개 후보를 드러내거나 피드백을 저장하지 않는 진단,
두 자료 연결과 이전·현재 기억의 수동 비교를 검증합니다. 통제된 회귀 사례이며
일반적인 작업 정확도를 주장하는 결과는 아닙니다. 벤치마크 전용 규칙은 없습니다.
표현이 다른 질문의 의미 검색, 자동 충돌 해결, 사건 시점 추론은 실제 이득을
확인한 뒤 판단할 별도 과제입니다.
