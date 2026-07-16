{{- define "graphblocks-operator.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "graphblocks-operator.controllerName" -}}
{{- printf "%s-controller" (printf "%s-%s" .Release.Name .Chart.Name | trunc 52 | trimSuffix "-") -}}
{{- end -}}

{{- define "graphblocks-operator.clusterFullname" -}}
{{- printf "%s-%s-%s" .Release.Namespace .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "graphblocks-operator.serviceAccountName" -}}
{{- default (include "graphblocks-operator.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{- define "graphblocks-operator.rbacRules" -}}
- apiGroups: ["graphblocks.ai"]
  resources:
    - graphreleases
    - graphdeployments
    - deploymentrevisions
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["apps"]
  resources:
    - deployments
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: [""]
  resources:
    - services
    - configmaps
    - events
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
{{- end -}}
