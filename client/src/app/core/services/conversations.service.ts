import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import {
  Conversation,
  CreateConversationRequest,
  FileStatusResponse,
  Message,
  UploadFileResponse
} from '../models';

@Injectable({ providedIn: 'root' })
export class ConversationsService {
  private readonly baseUrl = `${environment.apiBaseUrl}/conversations`;

  constructor(private readonly http: HttpClient) {}

  list(): Observable<Conversation[]> {
    return this.http.get<Conversation[]>(this.baseUrl);
  }

  get(conversationId: string): Observable<Conversation> {
    return this.http.get<Conversation>(`${this.baseUrl}/${conversationId}`);
  }

  create(payload: CreateConversationRequest = {}): Observable<Conversation> {
    return this.http.post<Conversation>(this.baseUrl, payload);
  }

  remove(conversationId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${conversationId}`);
  }

  getMessages(conversationId: string): Observable<Message[]> {
    return this.http.get<Message[]>(`${this.baseUrl}/${conversationId}/messages`);
  }

  uploadFile(conversationId: string, file: File): Observable<UploadFileResponse> {
    const formData = new FormData();
    formData.append('file', file, file.name);
    return this.http.post<UploadFileResponse>(
      `${this.baseUrl}/${conversationId}/files`,
      formData
    );
  }

  getFileStatus(conversationId: string, fileId: string): Observable<FileStatusResponse> {
    return this.http.get<FileStatusResponse>(
      `${this.baseUrl}/${conversationId}/files/${fileId}/status`
    );
  }

  /** `systemPrompt: null` clears the conversation's custom instruction back
   *  to "none" -- see shared/CONTRACTS.md "system_prompt". */
  updateSystemPrompt(conversationId: string, systemPrompt: string | null): Observable<Conversation> {
    return this.http.patch<Conversation>(`${this.baseUrl}/${conversationId}/system-prompt`, {
      system_prompt: systemPrompt ?? ''
    });
  }
}
