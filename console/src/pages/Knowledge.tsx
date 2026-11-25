import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Card, Empty, Input, List, Modal, Popconfirm, Progress,
  Space, Table, Tag, Tooltip, Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  CloudUploadOutlined, DatabaseOutlined, DeleteOutlined, DownloadOutlined,
  EyeOutlined, FileTextOutlined, ReloadOutlined, SearchOutlined,
} from "@ant-design/icons";
import { request, uploadFile, downloadFile } from "../services/api";
import { errorText } from "../services/errorText";
import { useToast } from "../components/Toast";

const FILES_API = "/api/z-torque/files";
const KNOWLEDGE_API = "/api/z-torque/knowledge";
const ALLOWED_EXTENSIONS = [".pdf", ".txt", ".md", ".csv", ".xlsx"];
const MAX_FILE_BYTES = 50 * 1024 * 1024;

interface KnowledgeFile {
  id: string;
  name: string;
  media_type: string;
  size_bytes: number;
  status: "UPLOADED" | "PARSED" | "PARSE_FAILED" | string;
  created_at: string;
}
interface KnowledgeStatus {
  search: "READY" | "DEGRADED" | "UNAVAILABLE" | string;
  fts5: boolean;
  citation_enforcement: boolean;
}
interface KnowledgeChunk {
  source: string;
  content: string;
  chunk_id?: string;
  page_no?: number | null;
  sheet?: string | null;
  row_no?: number | null;
}
interface SearchResult { state: string; chunks: KnowledgeChunk[]; }

function formatBytes(value: number): string {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}
function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
function fileStatus(status: string): { label: string; color: string } {
  switch (status.toUpperCase()) {
    case "PARSED": return { label: "已解析", color: "success" };
    case "PARSE_FAILED": return { label: "解析失败", color: "error" };
    case "UPLOADED": return { label: "处理中", color: "processing" };
    default: return { label: status || "未知", color: "default" };
  }
}
function knowledgeState(state: string): { label: string; color: string; description: string } {
  switch (state.toUpperCase()) {
    case "AVAILABLE":
    case "READY": return { label: "检索可用", color: "success", description: "本地知识检索已就绪，上传的资料可用于知识检索。" };
    case "DEGRADED": return { label: "检索降级", color: "warning", description: "资料仍可上传和解析，但本地检索能力不完整，请检查本机索引环境。" };
    default: return { label: "检索不可用", color: "error", description: "文件管理仍可使用，但知识检索暂不可用；请检查本机知识库组件配置。" };
  }
}

export function KnowledgePage() {
  const { push: toast } = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<KnowledgeChunk[] | null>(null);
  const [preview, setPreview] = useState<{ name: string; content: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (quiet) setRefreshing(true);
    else setLoading(true);
    try {
      const [fileRows, knowledgeStatus] = await Promise.all([
        request<KnowledgeFile[]>(FILES_API),
        request<KnowledgeStatus>(`${KNOWLEDGE_API}/status`),
      ]);
      setFiles(fileRows);
      setStatus(knowledgeStatus);
    } catch (err) {
      toast("error", errorText(err, "加载知识库状态失败"));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [toast]);
  useEffect(() => { void load(); }, [load]);

  async function handleUpload(fileList?: FileList | null) {
    const selected = Array.from(fileList ?? []);
    if (selected.length === 0) return;
    const accepted: File[] = [];
    const rejected: string[] = [];
    for (const file of selected) {
      const extension = "." + (file.name.split(".").pop()?.toLowerCase() ?? "");
      if (!ALLOWED_EXTENSIONS.includes(extension)) rejected.push(file.name + "：格式不支持");
      else if (file.size > MAX_FILE_BYTES) rejected.push(file.name + "：超过 50 MB");
      else accepted.push(file);
    }
    if (rejected.length > 0) toast("info", "已跳过 " + rejected.length + " 个不符合要求的文件");
    if (accepted.length === 0) {
      if (inputRef.current) inputRef.current.value = "";
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    let succeeded = 0;
    for (let index = 0; index < accepted.length; index += 1) {
      const file = accepted[index];
      try {
        await uploadFile<KnowledgeFile>(FILES_API, file, {
          onProgress: (percent) => setUploadProgress(Math.round(((index + percent / 100) / accepted.length) * 100)),
        });
        succeeded += 1;
      } catch (err) {
        toast("error", file.name + "：" + errorText(err, "上传失败"));
      }
    }
    if (succeeded > 0) toast("success", "已上传 " + succeeded + " 份资料，系统开始解析并建立索引");
    await load(true);
    setUploading(false);
    setUploadProgress(0);
    if (inputRef.current) inputRef.current.value = "";
  }
  async function openPreview(file: KnowledgeFile) {
    setPreviewLoading(true);
    try {
      const data = await request<{ id: string; content: string }>(`${FILES_API}/${file.id}/content`);
      setPreview({ name: file.name, content: data.content });
    } catch (err) {
      toast("error", errorText(err, "读取文件内容失败"));
    } finally {
      setPreviewLoading(false);
    }
  }
  async function removeFile(file: KnowledgeFile) {
    try {
      await request(`${FILES_API}/${file.id}`, { method: "DELETE" });
      toast("success", "资料已删除");
      setFiles((current) => current.filter((item) => item.id !== file.id));
      setResults(null);
    } catch (err) {
      toast("error", errorText(err, "删除资料失败"));
    }
  }
  async function searchKnowledge() {
    const text = query.trim();
    if (!text) { toast("info", "先输入要查找的问题或关键词"); return; }
    setSearching(true);
    setResults(null);
    try {
      const response = await request<SearchResult>(`${KNOWLEDGE_API}/search`, { method: "POST", body: { query: text, top_k: 8 } });
      setResults(response.chunks ?? []);
      if (response.state !== "AVAILABLE" && response.state !== "READY") toast("info", "检索服务当前处于降级状态，结果可能不完整");
    } catch (err) {
      toast("error", errorText(err, "知识检索失败"));
    } finally {
      setSearching(false);
    }
  }
  async function rebuildIndex() {
    setReindexing(true);
    try {
      const result = await request<{ indexed: number; mode: string }>(`${KNOWLEDGE_API}/reindex`, { method: "POST" });
      toast("success", `索引检查完成，处理 ${result.indexed ?? 0} 条资料`);
      await load(true);
    } catch (err) {
      toast("error", errorText(err, "重建索引失败"));
    } finally {
      setReindexing(false);
    }
  }

  const columns: ColumnsType<KnowledgeFile> = [
    {
      title: "资料名称", dataIndex: "name",
      render: (name: string) => <Space size={10}><span className="knowledge-file-icon"><FileTextOutlined /></span><Typography.Text strong ellipsis={{ tooltip: name }}>{name}</Typography.Text></Space>,
    },
    { title: "大小", dataIndex: "size_bytes", width: 110, render: formatBytes },
    { title: "状态", dataIndex: "status", width: 112, render: (value: string) => { const item = fileStatus(value); return <Tag color={item.color}>{item.label}</Tag>; } },
    { title: "添加时间", dataIndex: "created_at", width: 180, render: formatTime },
    {
      title: "操作", key: "actions", width: 142,
      render: (_: unknown, file) => (
        <Space size={4}>
          <Tooltip title="预览解析文本"><Button type="text" size="small" icon={<EyeOutlined />} disabled={file.status !== "PARSED"} onClick={() => void openPreview(file)} /></Tooltip>
          <Tooltip title="下载原文件"><Button type="text" size="small" icon={<DownloadOutlined />} onClick={() => void downloadFile(`${FILES_API}/${file.id}/raw`, file.name).catch((err) => toast("error", errorText(err, "下载失败")))} /></Tooltip>
          <Popconfirm title="确定删除这份资料？" description="删除后，它将不再参与后续知识检索。" onConfirm={() => void removeFile(file)}>
            <Tooltip title="删除资料"><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];
  const stateView = status ? knowledgeState(status.search) : null;

  return (
    <div className="knowledge-page">
      <div className="knowledge-header">
        <div>
          <Typography.Title level={3}>知识库</Typography.Title>
          <Typography.Paragraph type="secondary">管理本机资料，让智能体在回答问题时检索你授权的文件内容。</Typography.Paragraph>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load(true)} loading={refreshing}>刷新</Button>
          <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => inputRef.current?.click()} loading={uploading}>添加资料</Button>
          <input ref={inputRef} type="file" accept={ALLOWED_EXTENSIONS.join(",")} multiple hidden onChange={(event) => void handleUpload(event.target.files)} />
        </Space>
      </div>

      {stateView && (
        <Alert className="knowledge-state-alert" type={status?.search === "AVAILABLE" || status?.search === "READY" ? "success" : status?.search === "DEGRADED" ? "warning" : "error"} showIcon
          message={<Space><span>本地知识检索</span><Tag color={stateView.color}>{stateView.label}</Tag></Space>}
          description={stateView.description}
        />
      )}
      {uploading && <Card size="small" className="knowledge-upload-progress"><Space direction="vertical" style={{ width: "100%" }} size={6}><Typography.Text>正在上传资料…</Typography.Text><Progress percent={uploadProgress} size="small" /></Space></Card>}

      <div className="knowledge-search-grid">
        <Card className="knowledge-search-card" bordered>
          <div className="knowledge-card-heading">
            <div className="knowledge-card-icon"><SearchOutlined /></div>
            <div><Typography.Title level={5}>检索测试</Typography.Title><Typography.Text type="secondary">用真实问题检查资料是否能被找到</Typography.Text></div>
          </div>
          <Input.Search value={query} onChange={(event) => setQuery(event.target.value)} onSearch={() => void searchKnowledge()}
            placeholder="例如：项目的部署要求是什么？" enterButton="检索" loading={searching} size="large" />
          {results !== null && (
            <div className="knowledge-results">
              {results.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到相关内容，试试换一种问法或添加更多资料。" /> : (
                <List dataSource={results} renderItem={(item, index) => (
                  <List.Item key={item.chunk_id ?? `${item.source}-${index}`}>
                    <div className="knowledge-result-item">
                      <div className="knowledge-result-meta">
                        <Typography.Text strong>{item.source.split(/[\\/]/).pop() || item.source}</Typography.Text>
                        <Space size={6} wrap>
                          {item.page_no != null && <Tag>第 {item.page_no} 页</Tag>}
                          {item.sheet && <Tag>{item.sheet}</Tag>}
                          {item.row_no != null && <Tag>第 {item.row_no} 行</Tag>}
                        </Space>
                      </div>
                      <Typography.Paragraph className="knowledge-result-content">{item.content}</Typography.Paragraph>
                    </div>
                  </List.Item>
                )} />
              )}
            </div>
          )}
        </Card>

        <Card className="knowledge-summary-card" bordered>
          <div className="knowledge-card-heading"><div className="knowledge-card-icon"><DatabaseOutlined /></div><div><Typography.Title level={5}>资料概况</Typography.Title><Typography.Text type="secondary">当前本机知识资源</Typography.Text></div></div>
          <div className="knowledge-stat"><span>资料总数</span><strong>{files.length}</strong></div>
          <div className="knowledge-stat"><span>解析成功</span><strong>{files.filter((file) => file.status === "PARSED").length}</strong></div>
          <div className="knowledge-stat"><span>解析失败</span><strong>{files.filter((file) => file.status === "PARSE_FAILED").length}</strong></div>
          <div className="knowledge-summary-foot"><span>检索索引</span><Tag color={status?.fts5 ? "success" : "default"}>{status?.fts5 ? "SQLite FTS5" : "基础模式"}</Tag></div>
          <Button block icon={<ReloadOutlined />} loading={reindexing} disabled={status?.search === "UNAVAILABLE"} onClick={() => void rebuildIndex()}>检查并重建索引</Button>
        </Card>
      </div>

      <Card className="knowledge-files-card" bordered>
        <div className="knowledge-files-heading">
          <div><Typography.Title level={5}>我的资料</Typography.Title><Typography.Text type="secondary">上传的文件仅在当前用户授权范围内参与检索。</Typography.Text></div>
          <Tag>{files.length} 份资料</Tag>
        </div>
        <Table rowKey="id" columns={columns} dataSource={files} loading={loading}
          pagination={{ pageSize: 8, showSizeChanger: false, showTotal: (total) => `共 ${total} 份资料` }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有资料，添加 PDF、TXT、Markdown、CSV 或 Excel 文件开始构建知识库。" /> }}
          scroll={{ x: 720 }}
        />
      </Card>

      <Modal title={preview?.name ?? "资料预览"} open={!!preview || previewLoading} onCancel={() => setPreview(null)}
        footer={<Button onClick={() => setPreview(null)}>关闭</Button>} width={820} destroyOnClose>
        {previewLoading ? <Progress percent={100} status="active" showInfo={false} /> : <Input.TextArea value={preview?.content ?? ""} readOnly autoSize={{ minRows: 12, maxRows: 26 }} />}
      </Modal>
    </div>
  );
}
export default KnowledgePage;
