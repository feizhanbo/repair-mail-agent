import { Button, Form, Input, InputNumber, Select } from 'antd';
import type { TicketLine } from '../types/api';

export type TicketItemForm = {
  line_no?: number;
  material_code?: string;
  material_name?: string;
  board_code?: string;
  board_name?: string;
  sn?: string;
  quantity?: number;
  failure_description?: string;
  failure_information?: string;
  data_info?: string;
  remarks?: string;
  accessories?: string;
  manual_locked?: boolean;
};

const lockOptions = [
  { value: false, label: '未锁定' },
  { value: true, label: '人工锁定' },
];

type Props = {
  editingItem?: TicketLine | null;
  onSubmit: (values: TicketItemForm) => Promise<void>;
  loading: boolean;
  onCancel: () => void;
};

export default function TicketItemEditor({ editingItem, onSubmit, loading, onCancel }: Props) {
  const [form] = Form.useForm<TicketItemForm>();

  return (
    <Form<TicketItemForm>
      form={form}
      layout="vertical"
      initialValues={{
        line_no: editingItem?.line_no,
        material_code: editingItem?.material_code ?? undefined,
        material_name: editingItem?.material_name ?? undefined,
        board_code: editingItem?.board_code ?? undefined,
        board_name: editingItem?.board_name ?? undefined,
        sn: editingItem?.sn ?? undefined,
        quantity: editingItem?.quantity ?? 1,
        failure_description: editingItem?.failure_description ?? undefined,
        failure_information: editingItem?.failure_information ?? undefined,
        data_info: editingItem?.data_info ?? undefined,
        remarks: editingItem?.remarks ?? undefined,
        accessories: editingItem?.accessories ?? undefined,
        manual_locked: editingItem?.manual_locked ?? false,
      }}
      onFinish={onSubmit}
    >
      <div className="two-column-grid">
        <Form.Item label="行号" name="line_no"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
        <Form.Item label="数量" name="quantity"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
      </div>
      <Form.Item label="SN" name="sn"><Input /></Form.Item>
      <Form.Item label="SAP 物料代码（SN 主数据）" name="material_code"><Input /></Form.Item>
      <Form.Item label="SAP 物料名称（SN 主数据）" name="material_name"><Input /></Form.Item>
      <Form.Item label="板卡型号（邮件/附件）" name="board_code"><Input /></Form.Item>
      <Form.Item label="板卡名称（邮件/附件）" name="board_name"><Input /></Form.Item>
      <Form.Item label="故障描述" name="failure_description"><Input.TextArea rows={3} /></Form.Item>
      <Form.Item label="故障信息" name="failure_information"><Input.TextArea rows={2} /></Form.Item>
      <Form.Item label="数据信息" name="data_info"><Input.TextArea rows={2} /></Form.Item>
      <Form.Item label="配件" name="accessories"><Input /></Form.Item>
      <Form.Item label="备注" name="remarks"><Input.TextArea rows={2} /></Form.Item>
      <Form.Item label="人工锁定" name="manual_locked"><Select options={lockOptions} /></Form.Item>
      <Button type="primary" htmlType="submit" loading={loading}>保存</Button>
    </Form>
  );
}
