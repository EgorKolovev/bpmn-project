import React, { useState, useRef, useEffect } from "react";
import { HistoryEntry } from "../types";
import MessageBubble from "./MessageBubble";

const STARTER_PROMPTS = [
  {
    title: "Employee onboarding",
    desc: "HR approval, document collection, IT setup, orientation",
  },
  {
    title: "Order fulfillment",
    desc: "Order received, payment, packaging, shipping, delivery",
  },
  {
    title: "Loan application",
    desc: "Application, credit check, approval/rejection, disbursement",
  },
  {
    title: "Incident management",
    desc: "Report, triage, investigation, resolution, closure",
  },
];

interface ChatAreaProps {
  history: HistoryEntry[];
  onSendMessage: (text: string) => void;
  isLoading: boolean;
  sessionName: string | null;
}

const ArrowUpIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="19" x2="12" y2="5" />
    <polyline points="5 12 12 5 19 12" />
  </svg>
);

const ChatArea: React.FC<ChatAreaProps> = ({
  history,
  onSendMessage,
  isLoading,
  sessionName,
}) => {
  const [inputText, setInputText] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history]);

  const resizeTextarea = () => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = textarea.scrollHeight + "px";
  };

  useEffect(() => {
    resizeTextarea();
  }, [inputText]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = inputText.trim();
    if (!text || isLoading) return;
    onSendMessage(text);
    setInputText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleStarterClick = (prompt: typeof STARTER_PROMPTS[0]) => {
    if (isLoading) return;
    const text = `${prompt.title}: ${prompt.desc}`;
    onSendMessage(text);
  };

  return (
    <div className="chat-area">
      {sessionName && <div className="chat-header">{sessionName}</div>}
      <div className="messages-container">
        {history.length === 0 && (
          <div className="empty-chat">
            <h3>BPMN Generator</h3>
            <p>Describe a business process to generate a BPMN diagram</p>
            <div className="starter-prompts">
              {STARTER_PROMPTS.map((prompt, i) => (
                <div
                  key={i}
                  className="starter-prompt"
                  onClick={() => handleStarterClick(prompt)}
                >
                  <div className="starter-prompt-title">{prompt.title}</div>
                  <div className="starter-prompt-desc">{prompt.desc}</div>
                </div>
              ))}
            </div>
          </div>
        )}
        {history.map((entry, index) => (
          <MessageBubble key={index} entry={entry} />
        ))}
        {isLoading && (
          <div className="message message-assistant">
            <div className="message-content loading">
              <div className="loading-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      <form className="input-area" onSubmit={handleSubmit}>
        <textarea
          ref={textareaRef}
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            history.length === 0
              ? "Describe a business process..."
              : "Describe changes to the diagram..."
          }
          rows={1}
          disabled={isLoading}
        />
        <div className="send-btn-wrapper">
          <button
            type="submit"
            className="send-btn"
            disabled={isLoading || !inputText.trim()}
            title="Send message"
          >
            <ArrowUpIcon />
          </button>
        </div>
      </form>
    </div>
  );
};

export default ChatArea;
