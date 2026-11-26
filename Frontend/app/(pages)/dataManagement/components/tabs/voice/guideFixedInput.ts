// 운영자가 넣는 대기번호·창구 번호 고정값(2026-10-28 결정). 입력칸에 미리 채우고, 고쳐 넣을 수도 있다.
// Jetson 음성은 같은 값으로 채운 멘트("고객님의 대기번호는 10번입니다.")를 미리 합성해 둔다 —
// 바꾸면 Jetson /etc/robot-voice.env 의 FIXED_TICKET_NO·FIXED_COUNTER_NO 도 함께 바꾼다.
export const FIXED_TICKET_NO = "10";
export const FIXED_COUNTER_NO = "1";

/** 운영자 입력 단계(TICKET·COUNTER)의 입력칸 기본값. 그 밖의 단계는 빈칸. */
export function fixedInputFor(kind: string | null | undefined): string {
  if (kind === "TICKET") return FIXED_TICKET_NO;
  if (kind === "COUNTER") return FIXED_COUNTER_NO;
  return "";
}
