# Agent Memory

[English](README.md)

여러 AI 에이전트가 함께 사용할 수 있는 로컬 메모리·문서 검색 엔진입니다.
이 저장소를 포크하고 자신의 자료 경로를 설정하면 Codex, Claude Code, Hermes,
일반 CLI 또는 선택형 MCP에서 같은 자료를 검색할 수 있습니다.

저장소에는 실행 코드, 테스트, 가상 예제만 있습니다. 실제 자료, 기억 카드,
검색 기록, 인덱스는 별도의 비공개 폴더에 저장됩니다.

## 기반과 참고 프로젝트

공개본의 직접적인 코드 기반은 여러 에이전트가 같은 로컬 자료를 검색하도록
개발한 **Agent Memory 0.2 엔진**입니다. 기존 설계 문서에 기록된 외부 참고 자료와
실제 구현에 사용하는 라이브러리는 다음과 같습니다.

| 프로젝트·자료 | 관계 | 이 시스템에 반영한 부분 |
|---|---|---|
| [Karpathy의 LLM Wiki 패턴](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | 설계 참고 | 원본을 보존하고, 검토된 Markdown 기억·탐색 지도와 원문 근거를 분리하는 구조. |
| [Cerebras Knowledge Base](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base) | 기존 설계 문서의 참고 자료 | 문서 구간별 검색, 후보 순위 결합, 필요한 인접 구간 읽기. |
| [Cerebras Gist Memory](https://inference-docs.cerebras.ai/cookbook/agents/gist-memory)·[ReadAgent](https://arxiv.org/abs/2402.09727) | 설계 참고 | 작은 탐색 정보로 위치를 찾은 뒤 필요한 원문만 다시 읽는 흐름. |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | 저장·검색 기반 기술 | Python의 SQLite 연결을 통한 로컬 문서 목록, BM25 단어 검색, trigram 검색 인덱스. |
| [pypdfium2 / PDFium](https://github.com/pypdfium2-team/pypdfium2)·[pypdf](https://github.com/py-pdf/pypdf) | 실행 의존성 | PDF 텍스트 추출과 대체 파서. |
| [PyYAML](https://github.com/yaml/pyyaml) | 실행 의존성 | 기억 카드의 메타데이터 읽기. 추가 검증 규칙은 이 엔진에서 구현. |
| [FastEmbed](https://github.com/qdrant/fastembed) | 선택형 실험 의존성 | 분리된 의미 검색 실행 환경의 로컬 임베딩. |
| [qmd](https://github.com/tobi/qmd) | 비교한 대체 검색 엔진 | 로컬 검색·CLI·MCP 구조의 비교 대상. 현재 검색 엔진에 연결하지 않음. |

설계 참고 자료는 아이디어의 출처로 표시합니다. 해당 외부 저장소를 포크한 코드라는
뜻은 아닙니다. 현재 구현은 짧은 검색 결과와 선택한 원문 읽기를 사용합니다.
자동 wiki 편집과 LLM을 통한 gist 요약 생성은 구현 범위에 포함되지 않습니다.
Hermes, Codex, Claude Code는 공통 CLI를 사용하는 클라이언트입니다.

## Agent Memory에서 구현·추가한 부분

참고한 아이디어를 실제 로컬 엔진에서 사용할 수 있도록 다음 기능을 구현했습니다.
기존 알고리즘이나 기억 개념 자체를 새로 발명했다는 의미는 아닙니다.

| 영역 | 구현한 동작 |
|---|---|
| 원문 검증 | 원본 해시, 추출 정책 버전, 원본 버전에 묶인 `ref`를 검사해 자료·정책 변경 뒤 오래된 읽기를 차단. 원본 경로와 페이지·줄 위치 제공. |
| 영어·한국어 검색 | 단어 검색과 CJK trigram 후보의 순위를 RRF로 결합. Unicode 정규화로 문자 표현 차이 처리. |
| 읽기 예산 | 검색·원문 읽기·다중 문서 context에 UTF-8 바이트 예산 적용. 출처와 메타데이터도 예산에 포함. |
| 근거 탐색 | 긴 질문의 파일명·식별자를 보존하고 검색 경로를 진단. 같은 읽기 예산에서 질문에 맞는 원문 구간을 선택. [사용 흐름](docs/retrieval-workflows.ko.md) 참고. |
| 기억 정책 | 후보 수를 제한하기 전과 원문 읽기 때 검토 상태·프로젝트/분야 범위·민감도·유효기간 검사. 저장은 inbox 후보로 시작하며 승격은 수동. |
| 증분 색인 | 원본을 수정하지 않고 collection별 변경 반영. 여러 문서 형식과 선택형 Python AST 분할 지원. 파일 종류에 맞는 추출 버전 적용. |
| 최신성과 저장 | 로컬 동기화 시점과 외부 사실의 유효성을 분리. 불완전한 탐색에서 미방문 자료 보존. 소유자 전용 저장 권한, SQLite/FTS 삭제 처리, 활성 인덱스 정리 지원. |
| 검색 실패 개선 | 선택형 실패 기록과 명시적 관련성 보고에 중복 제거·보존 한도·비밀정보 필터 적용. 평가용 내보내기는 비활성 후보로 생성. |
| 공통 인터페이스 | 여러 에이전트가 같은 CLI·스킬 사용. 선택형 MCP에 목록·검색·읽기 도구 제공. 의미 검색은 별도 실험 기능으로 유지. |

RRF는 여러 검색 결과의 순위를 합치는 방법입니다. Trigram은 연속된 세 문자를
검색 단위로 쓰는 방식입니다. 비밀정보 필터는 모든 민감한 내용을 찾아내는 보장을
제공하지 않습니다.

### 공개 배포를 위해 이번에 추가한 부분

- **비공개 데이터 폴더 분리:** 저장소와 Python 패키지 설치 위치 밖에 데이터를
  저장하고 `AMEM_HOME`·`--home`으로 위치 지정.
- **첫 실행 설정:** `amem init`으로 기존 자료를 덮어쓰지 않는 초기 설정 생성.
  `--demo`에 한국어·영어 가상 문서와 평가 질문 세 개 제공.
- **패키지·연동 일반화:** 배포 패키지에 초기 설정 자료 포함. 기기 전용 경로 제거.
  여러 에이전트가 쓰는 공통 스킬 제공.
- **공개 파일 검사:** 운영 데이터의 Git 제외 규칙과 공개 소스 검사 도구 추가.
  검색 실패 자동 기록은 기본 비활성화.
- **기여·검증 환경:** 한국어·영어 안내, GitHub Actions 설정, 단위 테스트,
  임시 자료로 설치된 CLI·MCP를 확인하는 실행 스크립트 제공.
  특정 테스트 실행 방식에 의존하던 import 문제 수정.
- **의미 검색 범위 설정:** 기존 설치의 고정 연구 경로 대신 문서 collection과
  상대 디렉터리 범위를 선택하도록 변경.

## 설치와 첫 실행

macOS 또는 Linux, Python 3.9 이상, FTS5를 지원하는 SQLite 3.42.0 이상이 필요합니다.
Python이 사용하는 SQLite 버전을 확인해야 합니다. 별도의 `sqlite3` 명령만
업데이트해도 Python의 SQLite 버전이 바뀌지는 않습니다. 기본 런처와 파일 잠금은
Unix를 사용하므로 Windows에서는 WSL 같은 Linux 환경을 사용합니다.

포크한 저장소를 복제한 뒤 저장소 폴더에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)'
./amem init --demo
./amem doctor
./amem sync
./amem search '간격 반복 학습' --collection research --limit 3
./amem read '<검색 결과의 ref>' --max-tokens 1200
./amem eval
```

`init --demo`는 `~/.local/share/agent-memory`에 비공개 데이터 폴더를 만듭니다.
가상 문서 두 개와 평가 질문 세 개로 설치를 확인합니다. 기존 데이터가 있는 폴더를
덮어쓰지 않습니다. 예제 없는 빈 설치는 `./amem init`을 사용합니다.
예제 평가는 기본 동작 확인용이며 실제 자료의 검색 품질을 보장하지 않습니다.

다른 데이터 폴더를 쓰려면 `--home`을 하위 명령 **앞에** 넣습니다.
환경 변수 `AMEM_HOME`으로 지정할 수도 있습니다. `--home`이 우선합니다.

```bash
./amem --home /absolute/path/to/private-memory init --demo
./amem --home /absolute/path/to/private-memory sync
```

## 자신의 자료 연결

비공개 데이터 폴더의 `config.json`에서 `sources`를 수정합니다.
[`설정 예제`](src/agent_memory/resources/config.example.json)의 `research` 경로를
자신의 자료 폴더로 바꾸거나 새 collection을 추가합니다.
경로는 절대 경로, `~` 경로, 데이터 폴더 기준 상대 경로를 사용할 수 있습니다.

```bash
./amem sync --collection research
./amem search '찾으려는 내용' --collection research --limit 3
./amem read '<검색 결과의 ref>' --max-tokens 1200
```

원본 파일은 읽기만 합니다. 검색 결과는 탐색 힌트입니다. 선택한 `ref`로 원문을
읽은 뒤 답변의 근거로 사용합니다. 원본이 바뀌면 이전 `ref`는 사용할 수 없습니다.
Markdown, 텍스트, 코드, HTML, PDF, DOCX, PPTX, XLSX, HWPX를 지원합니다.
스캔 PDF는 먼저 OCR로 텍스트를 만들어야 합니다.

## 기억 저장과 연동

사용자가 저장을 요청한 내용만 `amem capture`로 inbox에 넣습니다.
기본 검색은 검토된 전역 기억만 사용합니다. 검토와 승격은 수동입니다.
대화를 자동 저장하지 않으며, 검색 실패 기록도 기본 비활성화입니다.

- [에이전트·MCP·정기 실행 연동](integrations/README.md)
- [기억 검토와 승격](docs/memory-lifecycle.md)
- [기억 카드 형식](docs/memory-schema.md)
- [시스템 구조](docs/architecture.md)
- [실험적 의미 검색](docs/semantic-pilot.md)

코어 CLI에는 모델 API 키가 필요 없습니다. 자료를 외부 서버에 올리지 않습니다.
`sync`는 로컬 파일을 반영하며 외부 웹사이트의 최신 사실을 확인하지 않습니다.
`--max-tokens`의 예산 단위는 실제 모델 토큰이 아니라 보수적인 UTF-8 바이트입니다.

## 검증

[LongMemEval-S 검색 벤치마크](benchmarks/README.ko.md)는 고정된 데이터로 LLM 없이
실행합니다. 같은 청크의 BM25 단어 검색과 비교하고, 실제 CLI 출력 예산의 영향도
측정합니다. 공개 점수는 필요한 대화 세션을 찾는 성능이며, 최종 답변 정확도가 아닙니다.
[기록된 검색 실험](benchmarks/IMPROVEMENTS.ko.md)은 당시 엔진의 점수와 조건을
보존합니다. 이 점수는 정답 구간의 실제 포함 여부나 작업 성공률을 측정하지 않습니다.
변경은 실제 작업에서 도움이 되는지를 확인해 채택합니다. 벤치마크 점수 상승만으로
반영하지 않습니다. [실사용 중심 검토](docs/practical-memory-review.ko.md)에 근거와
다음 검증 대상을 정리했습니다.

```bash
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python scripts/check_public_tree.py
```

테스트는 임시 폴더의 가상 자료를 사용합니다. 실제 설정·기억·인덱스·모델·로그는
이 저장소와 Git 기록에 넣지 않습니다. 공개 코드에 기여하는 방법은
[CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

## 라이선스

[MIT](LICENSE). 연결하는 문서와 별도로 설치하는 모델의 권리·라이선스는
해당 자료의 조건을 따릅니다.
