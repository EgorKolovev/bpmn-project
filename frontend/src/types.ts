export interface SessionInfo {
  session_id: string;
  name: string;
}

export interface HistoryEntry {
  role: "user" | "assistant";
  text?: string;
  bpmn_xml?: string;
}

export interface InitData {
  action: "init_data";
  user_id: string;
  session_token: string;
  sessions: SessionInfo[];
}

export interface SessionData {
  action: "session_data";
  session_id: string;
  name: string;
  bpmn_xml: string;
  history: HistoryEntry[];
}

export interface ResultData {
  action: "result";
  bpmn_xml: string;
  session_id?: string;
  session_name?: string;
}

export interface ErrorData {
  action: "error";
  message: string;
}

export type ServerEvent = InitData | SessionData | ResultData | ErrorData;
