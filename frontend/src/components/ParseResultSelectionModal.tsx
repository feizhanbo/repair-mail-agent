import { Checkbox, Empty, Modal, Space, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { ParseResult } from '../types/api';
import JsonBlock from './JsonBlock';

type Props = {
  open: boolean;
  parseResult: ParseResult | null;
  loading?: boolean;
  onCancel: () => void;
  onConfirm: (selection: { selected_fields: string[]; selected_item_indices: number[] }) => void;
};

export default function ParseResultSelectionModal({ open, parseResult, loading, onCancel, onConfirm }: Props) {
  const fields = useMemo(() => Object.keys(parseResult?.extracted_fields ?? {}), [parseResult]);
  const items = useMemo(() => {
    const extracted = parseResult?.extracted_items as unknown;
    if (Array.isArray(extracted)) return extracted;
    if (extracted && typeof extracted === 'object' && Array.isArray((extracted as { items?: unknown[] }).items)) {
      return (extracted as { items: unknown[] }).items;
    }
    return [];
  }, [parseResult]);
  const [selectedFields, setSelectedFields] = useState<string[]>([]);
  const [selectedItems, setSelectedItems] = useState<number[]>([]);

  useEffect(() => {
    if (open) {
      setSelectedFields(fields);
      setSelectedItems(items.map((_, index) => index));
    }
  }, [fields, items, open]);

  return (
    <Modal
      open={open}
      title="部分采纳字段与明细"
      okText="采纳所选"
      cancelText="取消"
      confirmLoading={loading}
      okButtonProps={{ disabled: selectedFields.length === 0 && selectedItems.length === 0 }}
      onCancel={onCancel}
      onOk={() => onConfirm({ selected_fields: selectedFields, selected_item_indices: selectedItems })}
      width={760}
    >
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Text strong>字段</Typography.Text>
          {fields.length ? (
            <Checkbox.Group
              style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', marginTop: 8 }}
              options={fields.map((field) => ({ label: field, value: field }))}
              value={selectedFields}
              onChange={(values) => setSelectedFields(values.map(String))}
            />
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可采纳字段" />}
        </div>
        <div>
          <Typography.Text strong>维修明细</Typography.Text>
          {items.length ? items.map((item, index) => (
            <div key={index} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', marginTop: 8 }}>
              <Checkbox
                checked={selectedItems.includes(index)}
                onChange={(event) => setSelectedItems((current) => (
                  event.target.checked ? [...current, index] : current.filter((value) => value !== index)
                ))}
              >明细 {index + 1}</Checkbox>
              <div style={{ flex: 1 }}><JsonBlock value={item} /></div>
            </div>
          )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可采纳明细" />}
        </div>
      </Space>
    </Modal>
  );
}
