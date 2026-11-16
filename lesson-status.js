'use strict';
function lessonFreshness(data, status, episodes, now = Date.now()) {
  if (!data?.episode) return '';
  const published = Date.parse(data.episode.date);
  const newer = episodes.some(ep => Date.parse(ep.date) > published);
  const failed = status?.status === 'failed' &&
    Date.parse(status.attempted_at) > Date.parse(data.generated_at);
  const old = !Number.isFinite(published) || now - published > 18 * 60 * 60 * 1000;
  if (failed) return '최신 학습자료 갱신에 실패해 이전 자료를 보존했습니다. 현재 문제는 최신 회차가 아닐 수 있습니다.';
  if (newer || old) return '이전 방송의 학습자료입니다. 최신 회차의 딕테이션 문제는 아직 준비되지 않았습니다.';
  return '표시된 방송의 학습자료입니다. NPR은 수시로 새 회차를 공개하므로 방송 시각을 확인해 주세요.';
}
function lessonNeedsRefresh(data, status) {
  return status?.status === 'success' &&
    Date.parse(data.generated_at) < Date.parse(status.lesson_generated_at);
}
function aiReviewMessage(status) {
  if(!status)return 'AI 문맥 검토 상태를 아직 확인하지 못했습니다.';
  const states={needs_api_key:'API 키 연결 대기 · 이번 작업의 API 호출 없음',disabled:'AI 자동 문맥 검토 꺼짐',cached:'기존 AI 문맥 검토 결과 재사용 · 추가 API 호출 없음',success:'AI 문맥 검토 완료',daily_limit_reached:'하루 호출 한도에 도달해 추가 검토를 멈췄습니다.',monthly_limit_reached:'월 예산 한도에 도달해 추가 검토를 멈췄습니다.',review_failed:'AI 검토 실패 · 기존 문제와 단서를 유지했습니다.',accounting_error:'사용량 기록을 확인할 수 없어 API 호출을 중단했습니다.'};
  let text=states[status.state]||'AI 문맥 검토 상태 확인 중';
  const total=Number(status.month_accounted_usd),cap=Number(status.monthly_limit_usd);
  if(Number.isFinite(total)&&Number.isFinite(cap))text+=' · '+status.month+' 사용·예약액 추정 $'+total.toFixed(4)+' / 앱 월 한도 $'+cap.toFixed(2);
  if(Number.isInteger(status.month_call_count))text+=' · 이번 달 호출 '+status.month_call_count+'회, 확인된 토큰: 입력 '+status.month_input_tokens+', 출력 '+status.month_output_tokens;
  if(status.last_call?.usage)text+=' · 최근 호출 토큰: 입력 '+status.last_call.usage.input_tokens+', 출력 '+status.last_call.usage.output_tokens;
  return text+' · 크레딧 차감 여부와 실제 현금 청구액은 API 계정에서 확인해야 합니다.';
}
if (typeof module !== 'undefined') module.exports = {lessonFreshness, lessonNeedsRefresh, aiReviewMessage};
