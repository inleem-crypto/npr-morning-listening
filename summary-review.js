'use strict';
function summaryNoteState(saved, episodeId) {
  if (!episodeId) return saved || {episodeId:'',text:''};
  return saved?.episodeId === episodeId ? saved : {episodeId,text:''};
}
function articleReference(references, ordinal) {
  const reference=references?.find((item,index)=>item && (item.article ?? index+1)===ordinal);
  return reference?.transcript?.trim() ? reference : null;
}
function transcriptReference(references, fullTranscript, ordinal) {
  return articleReference(references,ordinal)
    || (fullTranscript?.transcript?.trim() ? {...fullTranscript,kind:'full_broadcast'} : null);
}
function reviewSummary(text, reference) {
  const clean=String(text||'').trim();
  if (!clean) return {empty:true,message:'먼저 요약을 입력해 주세요.'};
  if (!reference?.keywords?.length) return {unavailable:true,message:'이 방송의 요약 기준을 준비하지 못했습니다. 학습자료가 있는 방송을 선택해 주세요.'};
  const normalized=clean.toLocaleLowerCase();
  const hasTerm=term=> /[가-힣]/.test(term) ? normalized.includes(term) : new RegExp('(^|[^a-z0-9])'+term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'([^a-z0-9]|$)','i').test(normalized);
  const matched=reference.keywords.filter(k=>k.terms.some(hasTerm));
  const missing=reference.keywords.filter(k=>!matched.includes(k));
  const count=clean.split(/[.!?。！？\n]+/).map(s=>s.trim()).filter(Boolean).length;
  const units=/[가-힣]/.test(clean)?clean.replace(/\s/g,'').length:clean.split(/\s+/).length;
  const concise=units>=12&&units<=300;
  const coverage=Math.round(70*matched.length/reference.keywords.length);
  const structure=count>=2&&count<=3?20:count===1||count===4?10:0;
  return {score:coverage+structure+(concise?10:0),coverage,structure,concise,count,
    matched:matched.map(k=>k.label),missing:missing.map(k=>k.label),
    caveat:'핵심어와 형식 기준의 참고 점수입니다. 동의어·부정문·사실관계의 정확성을 완전히 판정하지 못합니다.'};
}
if(typeof module!=='undefined')module.exports={reviewSummary,summaryNoteState,transcriptReference,articleReference};
