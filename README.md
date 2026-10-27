NPR Morning Listening Lab v4

웹 주소: https://inleem-crypto.github.io/npr-morning-listening/

이 웹앱은 NPR News Now의 최신 음원을 공식 팟캐스트 피드에서 찾습니다. 본문부터 듣기,
속도 조절, 구간 반복, 듣기 메모를 지원합니다. 음원은 NPR의 원본 링크로 재생하며
저장소에 MP3를 보관하지 않습니다.

딕테이션 문제는 GitHub Actions의 "Prepare daily dictation lesson" 작업이 한국 시각
매일 05:15와 05:25에 최신 회차를 자동 전사해 lesson.json으로 만듭니다. 전사 결과와
정답은 사람이 검증한 대본이 아니므로 틀릴 수 있습니다. GitHub Actions 일정은
지연될 수 있으며 음원이나 전사 모델을 다운로드하지 못하면 기존 파일을 유지합니다.

웹앱은 lesson.json을 자동으로 읽고 해당 문제의 방송을 선택합니다. 문제 파일이
없거나 오래되었으면 준비 중이라는 메시지를 표시합니다. 수동 JSON 입력은 평소에
필요하지 않습니다.

배포 시 index.html, manifest.webmanifest, icon.svg, sw.js, requirements.txt,
scripts/generate_lesson.py, .github/workflows/daily-lesson.yml을 저장소 main 브랜치에
같은 상대 경로로 올리세요. GitHub Pages는 main / (root)로 둡니다. 첫 실행은
저장소 Actions 탭에서 Prepare daily dictation lesson을 선택하고 Run workflow를
누를 수 있습니다. 작업이 실패하면 해당 실행 기록에서 원인을 확인하세요.

전사 품질 관리 (생성기 v6)

- CPU int8 `small.en`으로 단어별 시각과 신뢰도를 함께 전사합니다.
- 낮은 단어 신뢰도, 무음, 반복, 불완전한 문장과 고유명사 위험 문장을 제외합니다.
  대문자와 기자 소개를 이용한 보수적인 검사이며 모든 인명을 식별하지는 못합니다.
- 최대 12개 후보의 원본 음성 구간을 `medium.en`으로 다시 전사합니다. 두 결과의
  단어와 숫자가 일치하고 두 번째 전사도 품질 검사를 통과해야 출제합니다.
- 인명 사전 치환이나 결과 자동 덮어쓰기는 하지 않습니다. 문제가 1~5개만 나올 수
  있으며, 통과 문장이 없거나 다운로드/전사에 실패하면 기존 문제를 유지합니다.
- 모델 간 일치는 사람의 청취 검증을 대신하지 않습니다. `verified: false`와 기존
  안내를 유지합니다. 품질 기준과 모델 이름은 scripts/generate_lesson.py에 있습니다.

검증: `python -m unittest discover -s tests -v`

원본 회차 재현 비교: `python scripts/audit_audio.py` 후
`python scripts/check_audit_candidates.py`를 실행합니다. 중단 후 재개할 때는
`python scripts/audit_audio.py --resume`을 사용합니다. 다른 회차는 별도의 작업
디렉터리에서 비교하세요. 내려받은 음원과 전체 전사 결과는 `.audit/`에만 저장하며
GitHub에 올리지 않습니다. 최초 모델 다운로드 시간은 실제 전사 시간과 구분합니다.

조사 근거와 대안 비교는 [전사 품질 조사](docs/transcription-quality.md)에 기록합니다.
