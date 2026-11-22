{{/*
Chart name used in labels.
*/}}
{{- define "chat-system.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "chat-system.labels" -}}
app.kubernetes.io/part-of: {{ include "chat-system.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Selector labels for a given component name, e.g. "gateway".
Usage: {{ include "chat-system.selectorLabels" (dict "component" "gateway" "ctx" $) }}
*/}}
{{- define "chat-system.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end -}}

{{/*
Full set of labels for a component (common + selector).
*/}}
{{- define "chat-system.componentLabels" -}}
{{ include "chat-system.labels" .ctx }}
{{ include "chat-system.selectorLabels" . }}
{{- end -}}

{{/*
Fully-qualified image reference, honoring global.imageRegistry.
Usage: {{ include "chat-system.image" (dict "repository" "chat-gateway" "tag" "0.1.0" "ctx" $) }}
*/}}
{{- define "chat-system.image" -}}
{{- $registry := .ctx.Values.global.imageRegistry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}
