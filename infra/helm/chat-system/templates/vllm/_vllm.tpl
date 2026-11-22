{{/*
Shared template for one of the 4 small vLLM model Deployments.
Usage: {{ include "chat-system.vllmDeployment" (dict "name" "vllm-embed" "model" .Values.vllm.embed "ctx" $) }}
*/}}
{{- define "chat-system.vllmDeployment" -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .name }}
  labels:
    {{- include "chat-system.componentLabels" (dict "component" .name "ctx" .ctx) | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "chat-system.selectorLabels" (dict "component" .name "ctx" .ctx) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "chat-system.selectorLabels" (dict "component" .name "ctx" .ctx) | nindent 8 }}
    spec:
      {{- with .ctx.Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ .name }}
          image: {{ include "chat-system.image" (dict "repository" .ctx.Values.vllm.image.repository "tag" .ctx.Values.vllm.image.tag "ctx" .ctx) }}
          imagePullPolicy: {{ .ctx.Values.global.imagePullPolicy }}
          # Air-gapped: force offline resolution from the pre-populated HF
          # cache mounted below — vllm must never attempt a network pull.
          env:
            - name: HF_HOME
              value: {{ .ctx.Values.vllm.modelWeights.mountPath | quote }}
            - name: HF_HUB_OFFLINE
              value: "1"
            - name: TRANSFORMERS_OFFLINE
              value: "1"
          command: ["vllm", "serve", {{ .model.modelId | quote }}]
          args:
            {{- toYaml .model.args | nindent 12 }}
            - "--port"
            - {{ .model.port | quote }}
          ports:
            - name: http
              containerPort: {{ .model.port }}
          resources:
            {{- toYaml .model.resources | nindent 12 }}
          volumeMounts:
            - name: model-weights
              mountPath: {{ .ctx.Values.vllm.modelWeights.mountPath }}
              readOnly: true
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 60
            periodSeconds: 15
            failureThreshold: 10
          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 120
            periodSeconds: 20
            failureThreshold: 6
      volumes:
        - name: model-weights
          persistentVolumeClaim:
            claimName: {{ .ctx.Values.vllm.modelWeights.pvc.name }}
{{- end -}}

{{/*
Shared template for a vLLM model's Service.
Usage: {{ include "chat-system.vllmService" (dict "name" "vllm-embed" "model" .Values.vllm.embed "ctx" $) }}
*/}}
{{- define "chat-system.vllmService" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ .name }}
  labels:
    {{- include "chat-system.componentLabels" (dict "component" .name "ctx" .ctx) | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "chat-system.selectorLabels" (dict "component" .name "ctx" .ctx) | nindent 4 }}
  ports:
    - name: http
      port: {{ .model.port }}
      targetPort: {{ .model.port }}
{{- end -}}
