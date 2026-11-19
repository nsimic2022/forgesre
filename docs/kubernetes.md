# Kubernetes, OpenShift and k3s

Two layers work together. Use both when you can.

1. **Metrics and alerts inside the cluster: the standard.** Prometheus, Alertmanager,
   kube-state-metrics and node-exporter, with the alert rules of the kubernetes-mixin project.
   On plain Kubernetes and k3s install `kube-prometheus-stack` (Helm) or `kube-prometheus`;
   OpenShift ships the same stack in `openshift-monitoring`. Point that Alertmanager at
   ForgeSRE and every cluster alert becomes a ForgeSRE incident with rules, noise filters,
   escalation and ForgeRCA.
2. **ForgeSRE's own cluster reader (this add-on).** A read-only service account lets ForgeSRE
   list nodes and workloads, put them in the inventory and the dependency map, raise a few
   basic alerts, and record every image change as a deploy under "What changed before". It
   works even before layer 1 exists, and it is how ForgeSRE knows what the alerts are about.

Data stays where it is: ForgeSRE reads the cluster API, and the cluster's Alertmanager posts
to ForgeSRE. Nothing goes to a third party.

## 1. Send the cluster's alerts to ForgeSRE

Add a webhook receiver to the cluster's Alertmanager:

```yaml
receivers:
  - name: forgesre
    webhook_configs:
      - url: https://forgesre.dc.local/api/v1/webhooks/alertmanager
        send_resolved: true
        http_config:
          authorization:
            credentials: <ALERTMANAGER_WEBHOOK_TOKEN from secrets/secrets.env>
route:
  receiver: forgesre
  group_by: [alertname, namespace]
```

On OpenShift: Administration › Cluster Settings › Configuration › Alertmanager › Create
Receiver (type Webhook), or edit the `alertmanager-main` secret in `openshift-monitoring`.
Keep "send resolved alerts" on, so incidents close by themselves.

ForgeSRE attaches an alert to an asset by its `asset` label. Give cluster alerts the cluster's
asset id (`k8s-<name>`, created by layer 2) with an alert relabel rule in the cluster's
Prometheus, for example in kube-prometheus-stack values:

```yaml
prometheus:
  prometheusSpec:
    additionalAlertRelabelConfigs:
      - target_label: asset
        replacement: k8s-prod        # the name you give the cluster in ForgeSRE
```

## 2. Let ForgeSRE read the cluster

Create a read-only service account (works on Kubernetes, k3s and OpenShift):

```yaml
apiVersion: v1
kind: ServiceAccount
metadata: {name: forgesre-reader, namespace: kube-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: forgesre-reader}
rules:
  - apiGroups: [""]
    resources: [nodes, pods, namespaces]
    verbs: [get, list]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets]
    verbs: [get, list]
  - apiGroups: [config.openshift.io]      # OpenShift only; harmless elsewhere
    resources: [clusteroperators]
    verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: forgesre-reader}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: forgesre-reader}
subjects: [{kind: ServiceAccount, name: forgesre-reader, namespace: kube-system}]
```

```bash
kubectl create token forgesre-reader -n kube-system --duration=8760h   # or a long-lived token Secret
```

Add the cluster to `config/forgesre.yml` and store the token on the ForgeSRE host:

```yaml
kubernetes:
  clusters:
    - name: prod                 # asset id becomes k8s-prod
      url: https://10.0.0.10:6443
      ca_pem: |                  # the cluster CA (kubeconfig certificate-authority-data, base64-decoded)
        -----BEGIN CERTIFICATE-----
        ...
        -----END CERTIFICATE-----
      # omit ca_pem to use the system CAs; insecure: true only for a lab
```

```bash
./forgesre settings set-secret K8S_TOKEN_PROD      # name upper-cased, other characters become _
./forgesre kubernetes sync && sleep 30 && ./forgesre kubernetes
```

ForgeSRE reads every cluster every 5 minutes (background job, 10 s timeout per request, GET
only) and:

| What | How |
|---|---|
| Assets | `Kubernetes cluster` (`k8s-<name>`), `Kubernetes node` (with its internal IP), `Kubernetes workload` (`namespace/name`, namespace in the customer field). Nodes and workloads have the cluster as parent, so an unreachable API gives one incident. Workloads have no IP and no scrape address: nothing pings or scrapes them. |
| Alerts | `KubeApiUnreachable` (critical), `KubeNodeNotReady` (critical), `KubeWorkloadUnavailable` (ready below desired, high), `KubePodCrashLooping` (CrashLoopBackOff, ImagePullBackOff, high), `OpenShiftClusterOperatorDegraded` (high). They resolve by themselves when healthy again. |
| Changes | A workload whose image changed is recorded as a deploy, shown on incidents under "What changed before". |

These alerts are deliberately few. Detailed alerts (CPU throttling, PVC filling up,
certificate expiry, etcd) come from layer 1.

Health: `./forgesre doctor` (Kubernetes row), `./forgesre kubernetes`, `GET /api/v1/kubernetes`.

## Docker without Kubernetes

A host that runs plain Docker is monitored as a Linux server (node_exporter) today.
Per-container metrics and alerts (cAdvisor) are on the roadmap.
