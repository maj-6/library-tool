package org.whl.bookcapture

import android.app.Activity
import android.view.LayoutInflater
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog

/**
 * The CH merge preview.
 *
 * Shown right after an approval so the merge is never invisible, and reachable
 * again from an already-approved indicator so it stays auditable. Fields the
 * merge took from CH are marked three ways — a coloured stripe, a "CH" tag, and
 * their position in the adopted group — because colour alone is not an
 * accessible signal.
 */
internal object ChMergeDialog {

    fun show(activity: Activity, preview: ChMergePreview, onUndo: (() -> Unit)? = null) {
        val view = LayoutInflater.from(activity).inflate(R.layout.dialog_ch_merge, null, false)
        val candidate = preview.candidate

        view.findViewById<TextView>(R.id.mergeCandidateTitle).text =
            candidate?.title.orEmpty().ifEmpty { activity.getString(R.string.catalog_ch_label) }
        view.findViewById<TextView>(R.id.mergeCandidateSub).text = listOfNotNull(
            candidate?.author?.takeIf(String::isNotBlank),
            candidate?.year?.takeIf(String::isNotBlank),
        ).joinToString(" · ")

        view.findViewById<TextView>(R.id.mergeSummary).text = summary(activity, preview)

        val list = view.findViewById<LinearLayout>(R.id.mergeFieldList)
        val inflater = LayoutInflater.from(activity)
        // Adopted first: they are what changed, and burying them under a long
        // run of unchanged fields is what makes a diff useless.
        val ordered = preview.adopted + preview.conflicts +
            preview.fields.filter { it.origin == FieldOrigin.UNCHANGED }
        ordered.forEach { field -> list.addView(row(activity, inflater, list, field)) }

        val builder = AlertDialog.Builder(activity)
            .setView(view)
            .setPositiveButton(android.R.string.ok, null)
        if (onUndo != null) {
            builder.setNegativeButton(R.string.catalog_ch_undo) { _, _ -> onUndo() }
        }
        builder.show()
    }

    private fun summary(activity: Activity, preview: ChMergePreview): String {
        val parts = mutableListOf<String>()
        parts += activity.resources.getQuantityString(
            R.plurals.ch_merge_adopted, preview.adopted.size, preview.adopted.size,
        )
        if (preview.conflicts.isNotEmpty()) {
            parts += activity.resources.getQuantityString(
                R.plurals.ch_merge_conflicts, preview.conflicts.size, preview.conflicts.size,
            )
        }
        return parts.joinToString(" · ")
    }

    private fun row(
        activity: Activity,
        inflater: LayoutInflater,
        parent: LinearLayout,
        field: MergeField,
    ): View {
        val row = inflater.inflate(R.layout.item_ch_merge_field, parent, false)
        row.findViewById<TextView>(R.id.fieldLabel).text = field.label
        row.findViewById<TextView>(R.id.mergedValue).text = field.mergedValue

        val stripe = row.findViewById<View>(R.id.originStripe)
        val tag = row.findViewById<TextView>(R.id.originTag)
        val alternate = row.findViewById<TextView>(R.id.alternateValue)

        when (field.origin) {
            FieldOrigin.ADOPTED -> {
                stripe.setBackgroundColor(activity.getColor(R.color.whl_green))
                tag.visibility = View.VISIBLE
                tag.setText(R.string.ch_merge_tag_ch)
                tag.setTextColor(activity.getColor(R.color.whl_green))
                alternate.visibility = View.GONE
            }
            FieldOrigin.CONFLICT -> {
                stripe.setBackgroundColor(activity.getColor(R.color.whl_amber))
                tag.visibility = View.VISIBLE
                tag.setText(R.string.ch_merge_tag_kept)
                tag.setTextColor(activity.getColor(R.color.whl_amber))
                // Spell out that CH's value was NOT applied. A bare second line
                // would read as "merged to this".
                alternate.visibility = View.VISIBLE
                alternate.text = activity.getString(R.string.ch_merge_conflict_alt, field.chValue)
            }
            FieldOrigin.UNCHANGED -> {
                stripe.setBackgroundColor(activity.getColor(R.color.whl_blue2))
                tag.visibility = View.GONE
                alternate.visibility = View.GONE
            }
        }

        row.contentDescription = when (field.origin) {
            FieldOrigin.ADOPTED ->
                activity.getString(R.string.ch_merge_a11y_adopted, field.label, field.mergedValue)
            FieldOrigin.CONFLICT ->
                activity.getString(
                    R.string.ch_merge_a11y_conflict, field.label, field.mergedValue, field.chValue,
                )
            FieldOrigin.UNCHANGED ->
                activity.getString(R.string.ch_merge_a11y_unchanged, field.label, field.mergedValue)
        }
        return row
    }
}
