import { io, Socket } from "socket.io-client";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

const socket: Socket = io(BACKEND_URL, {
  transports: ["websocket", "polling"],
  autoConnect: false,
  auth: (callback) =>
    callback({
      user_id: localStorage.getItem("bpmn_user_id"),
      session_token: localStorage.getItem("bpmn_session_token"),
    }),
});

export default socket;
