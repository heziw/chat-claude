import { Component, ElementRef, OnDestroy, OnInit, ViewChild, computed, signal, ChangeDetectionStrategy } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute } from '@angular/router';
import { ConversationsService } from '../../../core/services/conversations.service';
import { ChatHistoryTurn, ChatService, ThinkingLevel } from '../../../core/services/chat.service';
import { Conversation, IndexStatus, Message, ReasoningInfo, UploadingFile } from '../../../core/models';

@Component({
    selector: 'app-chat',
    imports: [FormsModule, NgTemplateOutlet],
    templateUrl: './chat.component.html',
    changeDetection: ChangeDetectionStrategy.Eager,
    styleUrl: './chat.component.scss'
})
export class ChatComponent implements OnInit, OnDestroy {
  @ViewChild('fileInput') fileInputRef?: ElementRef<HTMLInputElement>;
  @ViewChild('messagesEnd') messagesEndRef?: ElementRef<HTMLDivElement>;
  @ViewChild('questionInput') questionInputRef?: ElementRef<HTMLTextAreaElement>;

  readonly conversationId = signal<string | null>(null);
  readonly conversation = signal<Conversation | null>(null);
  readonly messages = signal<Message[]>([]);
  readonly isLoadingMessages = signal(false);
  readonly loadError = signal<string | null>(null);

  readonly questionText = signal('');
  readonly uploadingFiles = signal<UploadingFile[]>([]);
  readonly isDragOver = signal(false);

  /** User-selectable per question -- see shared/CONTRACTS.md "thinking_level".
   *  Persists across questions in this conversation view (not saved server-side). */
  readonly thinkingLevel = signal<ThinkingLevel>('standard');

  // ---- Conversation system-prompt settings panel ----
  readonly isSettingsOpen = signal(false);
  readonly systemPromptDraft = signal('');
  readonly isSavingSystemPrompt = signal(false);

  // ---- Image attachments (general-chat multimodal input) ----
  readonly attachedImages = signal<{ dataUrl: string; name: string }[]>([]);
  readonly imageAttachError = signal<string | null>(null);
  private static readonly MAX_IMAGES = 4;
  private static readonly MAX_IMAGE_BYTES = 4 * 1024 * 1024;

  /** Extensions that go through the ingestion/RAG pipeline (📎 upload) --
   *  everything else selected via the same picker that looks like an image
   *  (image/* MIME) is routed to the chat-attachment/vision flow instead
   *  (see shared/CONTRACTS.md "images"). tif/tiff count as documents here
   *  because they're the one image format the backend still OCRs (scanner
   *  output), unlike a camera photo or screenshot. */
  private static readonly DOCUMENT_EXTENSIONS = new Set([
    'pdf',
    'docx',
    'xlsx',
    'csv',
    'json',
    'tif',
    'tiff'
  ]);

  /** Built-in prompt templates for the "quick prompt" chips (see
   *  shared/CONTRACTS.md -- these are plain client-side text snippets, not
   *  a separate backend concept: clicking one just fills the composer, the
   *  usual routing/file-selection logic then takes it from there). */
  readonly quickPrompts: { label: string; text: string }[] = [
    { label: '📝 סכם', text: 'סכם את הקובץ שהעלית' },
    { label: '📊 נתח נתונים', text: 'נתח את הנתונים בקובץ וציין את הממצאים המרכזיים' },
    { label: '🌐 תרגם לאנגלית', text: 'תרגם את תוכן הקובץ לאנגלית' },
    { label: '🔑 נקודות מרכזיות', text: 'חלץ את נקודות המפתח העיקריות מהקובץ' }
  ];

  /** Key = `${messageId}:${citationIndex}` of the currently expanded citation snippet. */
  readonly expandedCitationKey = signal<string | null>(null);
  /** Message id of the currently expanded "הצג חשיבה" reasoning panel, if any
   *  (only one open at a time, same pattern as expandedCitationKey). */
  readonly expandedReasoningId = signal<string | null>(null);

  // Live streaming state, surfaced straight from ChatService's signals.
  readonly streamingText = computed(() => this.chatService.streamingText());
  readonly streamingCitations = computed(() => this.chatService.streamingCitations());
  readonly isStreaming = computed(() => this.chatService.isStreaming());
  /** Live "status" label (e.g. "מחפש ומנתח מידע רלוונטי..."), shown before
   *  real token text starts arriving; null once it does / once done. */
  readonly processingStatus = computed(() => this.chatService.processingStatus());
  /** Live `reasoning` trace for the in-flight answer, if the route emits
   *  one; captured into the final Message on `done` (see sendQuestion). */
  readonly streamingReasoning = computed(() => this.chatService.streamingReasoning());

  private pollTimers = new Map<string, ReturnType<typeof setInterval>>();

  constructor(
    private readonly route: ActivatedRoute,
    private readonly conversationsService: ConversationsService,
    readonly chatService: ChatService
  ) {}

  ngOnInit(): void {
    this.route.paramMap.subscribe((params) => {
      const id = params.get('id');
      this.conversationId.set(id);
      this.resetConversationState();
      if (id) {
        this.loadMessages(id);
        this.loadConversation(id);
      }
    });
  }

  private loadConversation(conversationId: string): void {
    this.conversationsService.get(conversationId).subscribe({
      next: (conversation) => {
        this.conversation.set(conversation);
        this.systemPromptDraft.set(conversation.system_prompt ?? '');
      },
      error: () => {
        /* non-critical for the chat view itself */
      }
    });
  }

  ngOnDestroy(): void {
    this.clearAllPolling();
    this.chatService.cancel();
  }

  private resetConversationState(): void {
    this.conversation.set(null);
    this.messages.set([]);
    this.loadError.set(null);
    this.uploadingFiles.set([]);
    this.expandedCitationKey.set(null);
    this.isSettingsOpen.set(false);
    this.systemPromptDraft.set('');
    this.attachedImages.set([]);
    this.imageAttachError.set(null);
    this.clearAllPolling();
    this.chatService.cancel();
  }

  private loadMessages(conversationId: string): void {
    this.isLoadingMessages.set(true);
    this.conversationsService.getMessages(conversationId).subscribe({
      next: (messages) => {
        this.messages.set(messages);
        this.isLoadingMessages.set(false);
        this.scrollToBottom();
      },
      error: () => {
        this.loadError.set('טעינת ההיסטוריה נכשלה');
        this.isLoadingMessages.set(false);
      }
    });
  }

  // ---- Sending questions / SSE streaming ----------------------------------

  sendQuestion(): void {
    const conversationId = this.conversationId();
    const question = this.questionText().trim();
    const images = this.attachedImages().map((img) => img.dataUrl);
    if (!conversationId || (!question && images.length === 0) || this.isStreaming()) return;

    const userMessage: Message = {
      id: `local-${Date.now()}`,
      conversation_id: conversationId,
      role: 'user',
      content: question,
      citations: [],
      attachments: images.length ? images : undefined,
      created_at: new Date().toISOString()
    };
    this.messages.update((list) => [...list, userMessage]);
    this.questionText.set('');
    this.attachedImages.set([]);
    this.scrollToBottom();

    const history = this.buildHistory();

    void this.chatService.sendQuery(
      conversationId,
      question,
      history,
      this.thinkingLevel(),
      {
        onDone: (finalText, citations) => {
          const assistantMessage: Message = {
            id: `local-${Date.now()}-assistant`,
            conversation_id: conversationId,
            role: 'assistant',
            content: finalText,
            citations,
            created_at: new Date().toISOString(),
            reasoning: this.chatService.streamingReasoning() ?? undefined
          };
          this.messages.update((list) => [...list, assistantMessage]);
          this.scrollToBottom();
        },
        onError: () => {
          this.scrollToBottom();
        }
      },
      images
    );
  }

  private buildHistory(): ChatHistoryTurn[] {
    return this.messages()
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }));
  }

  onQuestionKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendQuestion();
    }
  }

  // ---- Citations ------------------------------------------------------------

  citationKey(messageId: string, index: number): string {
    return `${messageId}:${index}`;
  }

  toggleCitation(key: string): void {
    this.expandedCitationKey.update((current) => (current === key ? null : key));
  }

  // ---- Reasoning panel --------------------------------------------------

  toggleReasoning(messageId: string): void {
    this.expandedReasoningId.update((current) => (current === messageId ? null : messageId));
  }

  /** Short Hebrew title for the reasoning toggle, route-specific so the
   *  user knows what kind of trace they're about to expand. */
  reasoningTitle(reasoning: ReasoningInfo): string {
    switch (reasoning.route) {
      case 'structured':
        return 'שלבי הניתוח';
      case 'summarize':
        return 'פרטי הסיכום';
      case 'multi':
        return 'פירוק השאלה';
      default:
        return 'חשיבת המודל';
    }
  }

  // ---- Conversation settings (custom system prompt) --------------------

  toggleSettings(): void {
    this.isSettingsOpen.update((open) => {
      if (!open) {
        // Re-sync the draft from the loaded conversation each time the
        // panel opens, in case it was saved from another tab/session.
        this.systemPromptDraft.set(this.conversation()?.system_prompt ?? '');
      }
      return !open;
    });
  }

  saveSystemPrompt(): void {
    const conversationId = this.conversationId();
    if (!conversationId || this.isSavingSystemPrompt()) return;

    this.isSavingSystemPrompt.set(true);
    const value = this.systemPromptDraft().trim() || null;
    this.conversationsService.updateSystemPrompt(conversationId, value).subscribe({
      next: (updated) => {
        this.conversation.set(updated);
        this.isSavingSystemPrompt.set(false);
        this.isSettingsOpen.set(false);
      },
      error: () => {
        this.isSavingSystemPrompt.set(false);
      }
    });
  }

  // ---- Quick prompt chips ------------------------------------------------

  applyQuickPrompt(text: string): void {
    this.questionText.update((current) => (current.trim() ? `${current}\n${text}` : text));
    // Let the user immediately continue typing/editing before sending --
    // these are templates to combine with (or send as-is), never auto-sent.
    setTimeout(() => this.questionInputRef?.nativeElement.focus(), 0);
  }

  // ---- Image attachments (general-chat multimodal input) -----------------

  removeAttachedImage(index: number): void {
    this.attachedImages.update((list) => list.filter((_, i) => i !== index));
  }

  private handleImageFiles(files: File[]): void {
    const remaining = ChatComponent.MAX_IMAGES - this.attachedImages().length;
    if (remaining <= 0) {
      this.imageAttachError.set(`ניתן לצרף עד ${ChatComponent.MAX_IMAGES} תמונות להודעה`);
      return;
    }

    files.slice(0, remaining).forEach((file) => {
      if (file.size > ChatComponent.MAX_IMAGE_BYTES) {
        this.imageAttachError.set(`"${file.name}" גדולה מדי (מקסימום 4MB)`);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        this.attachedImages.update((list) => [
          ...list,
          { dataUrl: reader.result as string, name: file.name }
        ]);
      };
      reader.onerror = () => {
        this.imageAttachError.set(`קריאת "${file.name}" נכשלה`);
      };
      reader.readAsDataURL(file);
    });
  }

  // ---- File upload + drag & drop --------------------------------------------

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    this.isDragOver.set(true);
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    this.isDragOver.set(false);
  }

  onDrop(event: DragEvent): void {
    event.preventDefault();
    this.isDragOver.set(false);
    const files = event.dataTransfer?.files;
    if (files) this.handleFiles(files);
  }

  openFilePicker(): void {
    this.fileInputRef?.nativeElement.click();
  }

  onFileInputChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (input.files) this.handleFiles(input.files);
    input.value = '';
  }

  /** Single entry point for the one "📎 attach" picker/drop-zone: routes
   *  each selected file by extension -- document-like extensions (see
   *  DOCUMENT_EXTENSIONS) go through the existing async ingestion/RAG
   *  pipeline; any other image goes through the synchronous chat-attachment
   *  (vision) flow instead; anything else is rejected with a clear error,
   *  since there's no third pipeline for it. */
  private handleFiles(files: FileList): void {
    this.imageAttachError.set(null);
    const imageFiles: File[] = [];

    Array.from(files).forEach((file) => {
      const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
      if (ChatComponent.DOCUMENT_EXTENSIONS.has(ext)) {
        this.uploadFile(file);
      } else if (file.type.startsWith('image/')) {
        imageFiles.push(file);
      } else {
        this.imageAttachError.set(`סוג הקובץ "${file.name}" אינו נתמך`);
      }
    });

    if (imageFiles.length) this.handleImageFiles(imageFiles);
  }

  private uploadFile(file: File): void {
    const conversationId = this.conversationId();
    if (!conversationId) return;

    const placeholder: UploadingFile = {
      fileId: `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      originalName: file.name,
      status: 'pending'
    };
    this.uploadingFiles.update((list) => [...list, placeholder]);

    this.conversationsService.uploadFile(conversationId, file).subscribe({
      next: (response) => {
        this.uploadingFiles.update((list) =>
          list.map((item) =>
            item === placeholder ? { ...item, fileId: response.file_id, status: 'pending' } : item
          )
        );
        this.startPolling(conversationId, response.file_id);
      },
      error: () => {
        this.uploadingFiles.update((list) =>
          list.map((item) =>
            item === placeholder
              ? { ...item, status: 'failed' as IndexStatus, errorMessage: 'העלאת הקובץ נכשלה' }
              : item
          )
        );
      }
    });
  }

  private startPolling(conversationId: string, fileId: string): void {
    const poll = () => {
      this.conversationsService.getFileStatus(conversationId, fileId).subscribe({
        next: (status) => {
          this.uploadingFiles.update((list) =>
            list.map((item) =>
              item.fileId === fileId
                ? { ...item, status: status.status, errorMessage: status.error }
                : item
            )
          );
          if (status.status === 'ready' || status.status === 'failed') {
            this.stopPolling(fileId);
          }
        },
        error: () => this.stopPolling(fileId)
      });
    };

    poll(); // immediate check, then every 2s until ready/failed
    const timer = setInterval(poll, 2000);
    this.pollTimers.set(fileId, timer);
  }

  private stopPolling(fileId: string): void {
    const timer = this.pollTimers.get(fileId);
    if (timer) {
      clearInterval(timer);
      this.pollTimers.delete(fileId);
    }
  }

  private clearAllPolling(): void {
    for (const timer of this.pollTimers.values()) {
      clearInterval(timer);
    }
    this.pollTimers.clear();
  }

  statusBadge(status: IndexStatus): string {
    if (status === 'ready') return '✅';
    if (status === 'failed') return '❌';
    return '⏳'; // every in-progress stage, known or not (see IndexStatus)
  }

  /** Named labels for the real sub-stages ingestion-service reports (see
   * CONTRACTS.md's index_status note); any other in-progress value (a
   * future-added stage this client doesn't know about yet) falls back to a
   * generic "מעבד..." rather than silently showing the raw status string. */
  private static readonly STATUS_LABELS: Record<string, string> = {
    pending: 'ממתין',
    parsing: 'מחלץ טקסט...',
    chunking: 'מחלק לקטעים...',
    embedding: 'מייצר embeddings...',
    indexing: 'כותב לאינדקס...',
    writing: 'שומר נתונים...',
    ready: 'מוכן',
    failed: 'נכשל'
  };

  statusLabel(status: IndexStatus): string {
    return ChatComponent.STATUS_LABELS[status] ?? 'מעבד...';
  }

  private scrollToBottom(): void {
    setTimeout(() => {
      this.messagesEndRef?.nativeElement.scrollIntoView({ behavior: 'smooth' });
    }, 0);
  }
}
