{{- define "graphblocks-deployment-chart.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "graphblocks-deployment-chart.scaffoldName" -}}
{{- printf "%s-controller-scaffold" (printf "%s-%s" .Release.Name .Chart.Name | trunc 43 | trimSuffix "-") -}}
{{- end -}}

{{- define "graphblocks-deployment-chart.clusterFullname" -}}
{{- printf "%s-%s-%s" .Release.Namespace .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "graphblocks-deployment-chart.serviceAccountName" -}}
{{- default (include "graphblocks-deployment-chart.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}
